from datetime import UTC, datetime, time

from companion_gateway.domain.medication import (
    FeishuSendResult,
    MedicationPlanCreate,
    MedicationOccurrenceStatus,
)
from companion_gateway.domain.executor import TaskDeliveryAttempt, TaskExecutor
from companion_gateway.domain.scheduler import TaskScheduler
from companion_gateway.domain.tasks import TaskStatus
from companion_gateway.medication.service import MedicationReminderService
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository


class FakeNotifier:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[tuple[str, str]] = []

    def send_text(self, *, text: str, trace_id: str) -> FeishuSendResult:
        self.calls.append((text, trace_id))
        return FeishuSendResult(
            success=self.success,
            message_id="om_fake_message" if self.success else None,
            error=None if self.success else "provider_error",
        )


def create_service(tmp_path, *, notifier: FakeNotifier | None = None):
    repository = SQLiteTaskRepository(tmp_path / "medication-service.db")
    repository.initialize()
    task_service = TaskService(repository)
    executor = TaskExecutor(task_service)
    service = MedicationReminderService(
        repository=repository,
        task_service=task_service,
        task_executor=executor,
        notifier=notifier or FakeNotifier(),
    )
    plan, _ = repository.create_medication_plan(
        MedicationPlanCreate(
            actor_id="voice-user",
            target_device_id="living-room",
            reminder_times=(time(8),),
            timezone="Asia/Shanghai",
            message="请确认今天早上的药已服用。",
        ),
        plan_id="med-plan-service",
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    return repository, task_service, executor, service, plan


def test_due_plan_creates_one_optional_idempotent_task(tmp_path) -> None:
    repository, _task_service, _executor, service, plan = create_service(tmp_path)
    now = datetime(2026, 8, 11, 0, tzinfo=UTC)

    first = service.tick(now=now, trace_id="trace-first")
    second = service.tick(now=now, trace_id="trace-second")
    occurrence = repository.list_medication_occurrences()[0]

    assert first.scheduled_task_ids == (occurrence.task_id,)
    assert second.scheduled_task_ids == ()
    assert occurrence.task_id is not None
    assert repository.get_medication_plan(plan.plan_id) is not None


def test_disabled_plan_does_not_create_occurrence_or_task(tmp_path) -> None:
    repository, _task_service, _executor, service, plan = create_service(tmp_path)
    repository.disable_medication_plan(
        plan.plan_id,
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    result = service.tick(
        now=datetime(2026, 8, 11, tzinfo=UTC),
        trace_id="trace-disabled",
    )

    assert result.created_occurrence_ids == ()
    assert repository.list_medication_occurrences() == []


def test_unacknowledged_occurrence_sends_one_fallback_after_ten_minutes(tmp_path) -> None:
    notifier = FakeNotifier()
    repository, task_service, executor, service, _plan = create_service(
        tmp_path,
        notifier=notifier,
    )
    due = datetime(2026, 8, 11, 0, tzinfo=UTC)
    service.tick(now=due, trace_id="trace-schedule")
    task_scheduler = TaskScheduler(
        executor=executor,
        deliver=lambda _task: True,
        interval_seconds=1,
    )
    task_scheduler.tick(now=due)

    delivered = service.tick(
        now=datetime(2026, 8, 11, 0, 9, tzinfo=UTC),
        trace_id="trace-delivered",
    )
    fallback = service.tick(
        now=datetime(2026, 8, 11, 0, 10, tzinfo=UTC),
        trace_id="trace-fallback",
    )
    repeated = service.tick(
        now=datetime(2026, 8, 11, 0, 11, tzinfo=UTC),
        trace_id="trace-repeated",
    )
    occurrence = repository.list_medication_occurrences()[0]

    assert delivered.delivered_occurrence_ids == (occurrence.occurrence_id,)
    assert fallback.fallback_occurrence_ids == (occurrence.occurrence_id,)
    assert repeated.fallback_occurrence_ids == ()
    assert len(notifier.calls) == 1
    assert "设备离线" not in notifier.calls[0][0]
    assert "语音提醒已发送" in notifier.calls[0][0]
    assert occurrence.status is MedicationOccurrenceStatus.ESCALATED
    assert repository.get_medication_occurrence(
        occurrence.occurrence_id
    ).feishu_message_id == "om_fake_message"
    assert task_service.get_task(occurrence.task_id).status is TaskStatus.DELIVERED


def test_undelivered_occurrence_sends_device_offline_fallback(tmp_path) -> None:
    notifier = FakeNotifier()
    repository, task_service, executor, service, _plan = create_service(
        tmp_path,
        notifier=notifier,
    )
    due = datetime(2026, 8, 11, 0, tzinfo=UTC)
    service.tick(now=due, trace_id="trace-schedule-offline")
    task_scheduler = TaskScheduler(
        executor=executor,
        deliver=lambda _task: False,
        interval_seconds=1,
    )
    task_scheduler.tick(now=due)

    fallback = service.tick(
        now=datetime(2026, 8, 11, 0, 10, tzinfo=UTC),
        trace_id="trace-fallback-offline",
    )
    occurrence = repository.list_medication_occurrences()[0]

    assert fallback.fallback_occurrence_ids == (occurrence.occurrence_id,)
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0].startswith("设备离线，语音通知失败")
    assert occurrence.status is MedicationOccurrenceStatus.ESCALATED
    assert task_service.get_task(occurrence.task_id).status is TaskStatus.PENDING_DELIVERY


def test_voice_synthesis_failure_uses_an_accurate_fallback(tmp_path) -> None:
    notifier = FakeNotifier()
    repository, task_service, executor, service, _plan = create_service(
        tmp_path,
        notifier=notifier,
    )
    due = datetime(2026, 8, 11, 0, tzinfo=UTC)
    service.tick(now=due, trace_id="trace-schedule-synthesis-failure")
    TaskScheduler(
        executor=executor,
        deliver=lambda _task: TaskDeliveryAttempt.failed("voice_synthesis_failed"),
        interval_seconds=1,
    ).tick(now=due)

    service.tick(
        now=datetime(2026, 8, 11, 0, 10, tzinfo=UTC),
        trace_id="trace-fallback-synthesis-failure",
    )

    assert len(notifier.calls) == 1
    assert "语音合成失败" in notifier.calls[0][0]
    assert "设备离线" not in notifier.calls[0][0]
    occurrence = repository.list_medication_occurrences()[0]
    assert task_service.get_task(occurrence.task_id).status is TaskStatus.PENDING_DELIVERY


def test_acknowledgement_is_idempotent_and_prevents_fallback(tmp_path) -> None:
    notifier = FakeNotifier()
    repository, _task_service, executor, service, plan = create_service(
        tmp_path,
        notifier=notifier,
    )
    due = datetime(2026, 8, 11, 0, tzinfo=UTC)
    service.tick(now=due, trace_id="trace-schedule")
    TaskScheduler(
        executor=executor,
        deliver=lambda _task: True,
        interval_seconds=1,
    ).tick(now=due)
    service.tick(now=due, trace_id="trace-adopt-delivery")
    occurrence = repository.list_medication_occurrences()[0]

    first = service.acknowledge_occurrence(
        occurrence.occurrence_id,
        actor_id=plan.actor_id,
        target_device_id=plan.target_device_id,
        occurred_at=datetime(2026, 8, 11, 0, 5, tzinfo=UTC),
        trace_id="trace-ack",
    )
    second = service.acknowledge_occurrence(
        occurrence.occurrence_id,
        actor_id=plan.actor_id,
        target_device_id=plan.target_device_id,
        occurred_at=datetime(2026, 8, 11, 0, 6, tzinfo=UTC),
        trace_id="trace-ack-duplicate",
    )
    service.tick(
        now=datetime(2026, 8, 11, 0, 10, tzinfo=UTC),
        trace_id="trace-after-ack",
    )

    assert first.status is MedicationOccurrenceStatus.ACKNOWLEDGED
    assert second.status is MedicationOccurrenceStatus.ACKNOWLEDGED
    assert len(notifier.calls) == 0
