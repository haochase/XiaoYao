from datetime import UTC, date, datetime, time, timedelta

import pytest

from companion_gateway.domain.medication import (
    FeishuFallbackStatus,
    MedicationOccurrence,
    MedicationOccurrenceStatus,
    MedicationPlanCreate,
)
from companion_gateway.storage.sqlite import SQLiteTaskRepository


def build_plan() -> MedicationPlanCreate:
    return MedicationPlanCreate(
        actor_id="voice-user",
        target_device_id="living-room",
        reminder_times=(time(8), time(20)),
        timezone="Asia/Shanghai",
        message="该吃药了，请确认已服药。",
    )


def build_occurrence(plan_id: str = "med-plan-1") -> MedicationOccurrence:
    scheduled_at = datetime(2026, 8, 11, 8, tzinfo=UTC)
    return MedicationOccurrence(
        occurrence_id="med-occurrence-1",
        plan_id=plan_id,
        actor_id="voice-user",
        target_device_id="living-room",
        local_date=date(2026, 8, 11),
        local_time=time(16),
        scheduled_at=scheduled_at,
        ack_deadline_at=scheduled_at + timedelta(minutes=10),
        status=MedicationOccurrenceStatus.SCHEDULED,
        feishu_status=FeishuFallbackStatus.PENDING,
        created_at=scheduled_at,
        trace_id="trace-medication",
    )


def test_medication_plan_and_occurrence_survive_repository_reinitialization(
    tmp_path,
) -> None:
    database_path = tmp_path / "medication.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()

    plan, created = repository.create_medication_plan(
        build_plan(),
        plan_id="med-plan-1",
        occurred_at=datetime(2026, 8, 11, 0, tzinfo=UTC),
    )
    occurrence, occurrence_created = repository.create_occurrence_if_absent(
        build_occurrence(plan.plan_id)
    )

    assert created is True
    assert occurrence_created is True

    restarted = SQLiteTaskRepository(database_path)
    restarted.initialize()

    assert restarted.get_medication_plan(plan.plan_id) == plan
    assert restarted.get_medication_occurrence(occurrence.occurrence_id) == occurrence


def test_duplicate_local_times_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        MedicationPlanCreate(
            actor_id="voice-user",
            target_device_id="living-room",
            reminder_times=(time(8), time(8)),
            timezone="Asia/Shanghai",
            message="take medicine",
        )


def test_duplicate_daily_occurrence_returns_existing_row(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "medication-duplicate.db")
    repository.initialize()
    plan, _ = repository.create_medication_plan(
        build_plan(),
        plan_id="med-plan-duplicate",
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    occurrence = build_occurrence(plan.plan_id)

    first, first_created = repository.create_occurrence_if_absent(occurrence)
    second, second_created = repository.create_occurrence_if_absent(occurrence)

    assert first == second == occurrence
    assert first_created is True
    assert second_created is False


def test_disabled_plan_is_not_listed_as_enabled_and_can_be_disabled(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "medication-disabled.db")
    repository.initialize()
    plan, _ = repository.create_medication_plan(
        build_plan(),
        plan_id="med-plan-disabled",
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    assert [item.plan_id for item in repository.list_medication_plans(enabled=True)] == [
        plan.plan_id
    ]
    disabled = repository.disable_medication_plan(
        plan.plan_id,
        occurred_at=datetime(2026, 8, 11, 1, tzinfo=UTC),
    )

    assert disabled.enabled is False
    assert repository.list_medication_plans(enabled=True) == []


def test_atomic_fallback_claim_and_completion(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "medication-fallback.db")
    repository.initialize()
    plan, _ = repository.create_medication_plan(
        build_plan(),
        plan_id="med-plan-fallback",
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    occurrence, _ = repository.create_occurrence_if_absent(build_occurrence(plan.plan_id))

    first_claim = repository.claim_feishu_fallback(occurrence.occurrence_id)
    second_claim = repository.claim_feishu_fallback(occurrence.occurrence_id)
    sent = repository.complete_feishu_fallback(
        occurrence.occurrence_id,
        status=FeishuFallbackStatus.SENT,
        message_id="om_test_message",
        error=None,
    )

    assert first_claim is True
    assert second_claim is False
    assert sent.feishu_status is FeishuFallbackStatus.SENT
    assert sent.feishu_message_id == "om_test_message"
