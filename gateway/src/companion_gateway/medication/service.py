from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.medication import (
    FeishuFallbackStatus,
    FeishuSendResult,
    MedicationOccurrence,
    MedicationOccurrenceStatus,
    MedicationPlan,
    MedicationTickResult,
)
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.domain.tasks import TaskEventType, TaskStatus
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class MedicationNotifier(Protocol):
    def send_text(self, *, text: str, trace_id: str) -> FeishuSendResult: ...


class UnconfiguredMedicationNotifier:
    def send_text(self, *, text: str, trace_id: str) -> FeishuSendResult:
        return FeishuSendResult(success=False, error="feishu_not_configured")


class MedicationReminderService:
    def __init__(
        self,
        *,
        repository: SQLiteTaskRepository,
        task_service: TaskService,
        task_executor: TaskExecutor,
        notifier: MedicationNotifier,
        clock: Clock = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self._repository = repository
        self._task_service = task_service
        self._task_executor = task_executor
        self._notifier = notifier
        self._clock = clock
        self._id_factory = id_factory

    def tick(
        self,
        *,
        now: datetime | None = None,
        trace_id: str | None = None,
    ) -> MedicationTickResult:
        current = now or self._clock()
        self._require_aware(current)
        trace = trace_id or f"trc_medication_tick_{uuid4().hex}"
        created_ids: list[str] = []
        scheduled_ids: list[str] = []
        delivered_ids: list[str] = []
        fallback_ids: list[str] = []
        errors: list[str] = []

        for plan in self._repository.list_medication_plans(enabled=True):
            occurrence_ids = self._ensure_today_occurrences(plan, current, trace)
            created_ids.extend(occurrence_ids)

        occurrences = self._repository.list_medication_occurrences(
            statuses=(
                MedicationOccurrenceStatus.SCHEDULED,
                MedicationOccurrenceStatus.DELIVERED,
                MedicationOccurrenceStatus.ESCALATED,
            )
        )
        for occurrence in occurrences:
            try:
                occurrence, task_created = self._ensure_task(
                    occurrence,
                    trace,
                    current,
                )
                if task_created and occurrence.task_id is not None:
                    scheduled_ids.append(occurrence.task_id)
                occurrence, became_delivered = self._adopt_task_state(occurrence)
                if became_delivered:
                    delivered_ids.append(occurrence.occurrence_id)
                if (
                    occurrence.status is not MedicationOccurrenceStatus.ACKNOWLEDGED
                    and occurrence.ack_deadline_at <= current
                    and occurrence.feishu_status is FeishuFallbackStatus.PENDING
                    and self._repository.claim_feishu_fallback(occurrence.occurrence_id)
                ):
                    result = self._notifier.send_text(
                        text=self._fallback_text(occurrence),
                        trace_id=trace,
                    )
                    self._repository.complete_feishu_fallback(
                        occurrence.occurrence_id,
                        status=(
                            FeishuFallbackStatus.SENT
                            if result.success
                            else FeishuFallbackStatus.FAILED
                        ),
                        message_id=result.message_id,
                        error=result.error,
                    )
                    fallback_ids.append(occurrence.occurrence_id)
            except Exception as exc:
                errors.append(type(exc).__name__)

        return MedicationTickResult(
            created_occurrence_ids=tuple(created_ids),
            scheduled_task_ids=tuple(scheduled_ids),
            delivered_occurrence_ids=tuple(delivered_ids),
            fallback_occurrence_ids=tuple(fallback_ids),
            errors=tuple(errors),
        )

    def acknowledge_occurrence(
        self,
        occurrence_id: str,
        *,
        actor_id: str,
        target_device_id: str,
        occurred_at: datetime,
        trace_id: str,
    ) -> MedicationOccurrence:
        self._require_aware(occurred_at)
        occurrence = self._repository.get_medication_occurrence(occurrence_id)
        if occurrence is None:
            raise KeyError(occurrence_id)
        if (
            occurrence.actor_id != actor_id
            or occurrence.target_device_id != target_device_id
        ):
            raise PermissionError("medication occurrence ownership mismatch")
        if occurrence.status is MedicationOccurrenceStatus.ACKNOWLEDGED:
            return occurrence
        task = None
        if occurrence.task_id is not None:
            task = self._task_service.get_task(occurrence.task_id)
            if task is not None and task.status not in {
                TaskStatus.DELIVERED,
                TaskStatus.ACKNOWLEDGED,
            }:
                raise ValueError("medication reminder has not been delivered")
        acknowledged, won_acknowledgement = (
            self._repository.claim_occurrence_acknowledgement(
                occurrence_id,
                occurred_at=occurred_at,
            )
        )
        if not won_acknowledgement:
            return acknowledged
        if task is not None and task.status is TaskStatus.DELIVERED:
            self._task_service.record_event(
                task.task_id,
                TaskEventType.ACKNOWLEDGED,
                reason="medication_voice_acknowledged",
                trace_id=trace_id,
            )
        try:
            self._notifier.send_text(
                text=self._acknowledgement_receipt_text(acknowledged),
                trace_id=trace_id,
            )
        except Exception:
            pass
        return acknowledged

    def disable_plan(
        self,
        plan_id: str,
        *,
        actor_id: str,
        target_device_id: str,
        occurred_at: datetime,
    ) -> MedicationPlan:
        self._require_aware(occurred_at)
        plan = self._repository.get_medication_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        if plan.actor_id != actor_id or plan.target_device_id != target_device_id:
            raise PermissionError("medication plan ownership mismatch")
        return self._repository.disable_medication_plan(
            plan_id,
            occurred_at=occurred_at,
        )

    def voice_context(
        self,
        *,
        actor_id: str,
        target_device_id: str,
    ) -> dict[str, tuple[str, ...]]:
        occurrences = self._repository.list_medication_occurrences(
            statuses=(
                MedicationOccurrenceStatus.SCHEDULED,
                MedicationOccurrenceStatus.DELIVERED,
                MedicationOccurrenceStatus.ESCALATED,
            )
        )
        active_occurrence_ids = tuple(
            occurrence.occurrence_id
            for occurrence in occurrences
            if occurrence.actor_id == actor_id
            and occurrence.target_device_id == target_device_id
        )
        active_plan_ids = tuple(
            plan.plan_id
            for plan in self._repository.list_medication_plans(enabled=True)
            if plan.actor_id == actor_id and plan.target_device_id == target_device_id
        )
        return {
            "occurrence_ids": active_occurrence_ids,
            "plan_ids": active_plan_ids,
        }

    def is_medication_task(self, task_id: str) -> bool:
        return self._repository.get_medication_occurrence_by_task_id(task_id) is not None

    def _ensure_today_occurrences(
        self,
        plan: MedicationPlan,
        now: datetime,
        trace_id: str,
    ) -> list[str]:
        zone = ZoneInfo(plan.timezone)
        local_date = now.astimezone(zone).date()
        created_ids: list[str] = []
        for reminder_time in plan.reminder_times:
            scheduled_local = datetime.combine(
                local_date,
                reminder_time,
                tzinfo=zone,
            )
            occurrence_id = self._occurrence_id(
                plan.plan_id,
                local_date,
                reminder_time,
            )
            occurrence = MedicationOccurrence(
                occurrence_id=occurrence_id,
                plan_id=plan.plan_id,
                actor_id=plan.actor_id,
                target_device_id=plan.target_device_id,
                local_date=local_date,
                local_time=reminder_time,
                scheduled_at=scheduled_local,
                ack_deadline_at=scheduled_local.replace(microsecond=0),
                created_at=now,
                trace_id=trace_id,
            ).model_copy(
                update={
                    "ack_deadline_at": scheduled_local.replace(microsecond=0)
                    + timedelta(minutes=10)
                }
            )
            _, created = self._repository.create_occurrence_if_absent(occurrence)
            if created:
                created_ids.append(occurrence_id)
        return created_ids

    def _ensure_task(
        self,
        occurrence: MedicationOccurrence,
        trace_id: str,
        now: datetime,
    ) -> tuple[MedicationOccurrence, bool]:
        if occurrence.task_id is not None:
            return occurrence, False
        if occurrence.scheduled_at > now.astimezone(UTC):
            return occurrence, False
        plan = self._repository.get_medication_plan(occurrence.plan_id)
        if plan is None:
            raise KeyError(occurrence.plan_id)
        command = TaskCreate(
            actor_id=occurrence.actor_id,
            target_device_id=occurrence.target_device_id,
            kind=TaskKind.REMINDER,
            schedule=TaskSchedule(
                at=occurrence.scheduled_at,
                timezone=plan.timezone,
            ),
            payload=TaskPayload(text=plan.message),
            confirmation_policy=ConfirmationPolicy.OPTIONAL,
            idempotency_key=(
                f"medication:{plan.plan_id}:"
                f"{occurrence.local_date:%Y%m%d}:{occurrence.local_time:%H%M}"
            ),
        )
        task, created = self._task_executor.create_and_schedule(
            command,
            trace_id=trace_id,
        )
        bound = self._repository.bind_occurrence_task(
            occurrence.occurrence_id,
            task_id=task.task_id,
        )
        return bound, created

    def _adopt_task_state(
        self,
        occurrence: MedicationOccurrence,
    ) -> tuple[MedicationOccurrence, bool]:
        if occurrence.task_id is None:
            return occurrence, False
        task = self._task_service.get_task(occurrence.task_id)
        if task is None:
            return occurrence, False
        if task.status is TaskStatus.ACKNOWLEDGED:
            return (
                self._repository.mark_occurrence_acknowledged(
                    occurrence.occurrence_id,
                    occurred_at=self._clock(),
                ),
                False,
            )
        if (
            task.status is TaskStatus.DELIVERED
            and occurrence.status is MedicationOccurrenceStatus.SCHEDULED
        ):
            return (
                self._repository.mark_occurrence_delivered(occurrence.occurrence_id),
                True,
            )
        return occurrence, False

    @staticmethod
    def _occurrence_id(plan_id: str, local_date: date, local_time: time) -> str:
        key = f"{plan_id}:{local_date.isoformat()}:{local_time:%H:%M}"
        return f"med_occ_{sha256(key.encode()).hexdigest()[:32]}"

    def _fallback_text(self, occurrence: MedicationOccurrence) -> str:
        if occurrence.status is MedicationOccurrenceStatus.SCHEDULED:
            failure_reason = None
            if occurrence.task_id is not None:
                events = self._task_service.get_events(occurrence.task_id)
                failure_reason = events[-1].reason if events else None
            if failure_reason == "voice_synthesis_failed":
                return (
                    f"语音合成失败，提醒未能投递：{occurrence.local_date.isoformat()} "
                    f"{occurrence.local_time:%H:%M}。请确认是否已服药。"
                )
            if failure_reason == "voice_synthesis_unavailable":
                return (
                    f"语音服务不可用，提醒未能投递：{occurrence.local_date.isoformat()} "
                    f"{occurrence.local_time:%H:%M}。请确认是否已服药。"
                )
            if failure_reason == "outbound_backpressure":
                return (
                    f"设备语音队列繁忙，提醒未能投递：{occurrence.local_date.isoformat()} "
                    f"{occurrence.local_time:%H:%M}。请确认是否已服药。"
                )
            return (
                f"设备离线，语音通知失败：{occurrence.local_date.isoformat()} "
                f"{occurrence.local_time:%H:%M}。请确认是否已服药。"
            )
        return (
            f"语音提醒已发送，但暂未收到服药确认：{occurrence.local_date.isoformat()} "
            f"{occurrence.local_time:%H:%M}。请确认是否已服药。"
        )

    @staticmethod
    def _acknowledgement_receipt_text(occurrence: MedicationOccurrence) -> str:
        return (
            f"已确认服药：{occurrence.local_date.isoformat()} "
            f"{occurrence.local_time:%H:%M}。"
        )

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
