from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskRecord,
)
from companion_gateway.domain.tasks import TaskEventType, TaskStatus
from companion_gateway.service import TaskService


@dataclass(frozen=True)
class TaskDeliveryAttempt:
    delivered: bool
    failure_reason: str | None = None

    @classmethod
    def succeeded(cls) -> "TaskDeliveryAttempt":
        return cls(delivered=True)

    @classmethod
    def failed(cls, reason: str) -> "TaskDeliveryAttempt":
        if not reason.strip():
            raise ValueError("delivery failure reason must not be empty")
        return cls(delivered=False, failure_reason=reason)


class TaskExecutor:
    """Policy boundary for validated task commands produced by a voice turn."""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    def create_and_schedule(
        self,
        command: TaskCreate,
        *,
        trace_id: str,
    ) -> tuple[TaskRecord, bool]:
        task, created = self._service.create_task(command, trace_id=trace_id)
        if not created:
            return task, False
        if command.confirmation_policy is ConfirmationPolicy.REQUIRED:
            awaiting_confirmation, _ = self._service.record_event(
                task.task_id,
                TaskEventType.AWAITING_CONFIRMATION,
                reason="task_confirmation_required",
                trace_id=trace_id,
            )
            return awaiting_confirmation, True
        scheduled, _ = self._service.record_event(
            task.task_id,
            TaskEventType.SCHEDULED,
            reason="task_scheduled",
            trace_id=trace_id,
        )
        return scheduled, True

    def execute_due(
        self,
        *,
        now: datetime,
        deliver: Callable[[TaskRecord], bool | TaskDeliveryAttempt],
        trace_id: str,
    ) -> list[TaskRecord]:
        delivered: list[TaskRecord] = []
        for task in self._service.list_due_tasks(now=now):
            if task.status is TaskStatus.SCHEDULED:
                task, _ = self._service.record_event(
                    task.task_id,
                    TaskEventType.DUE,
                    reason="task_due",
                    trace_id=trace_id,
                )
                task, _ = self._service.record_event(
                    task.task_id,
                    TaskEventType.PENDING_DELIVERY,
                    reason="task_delivery_pending",
                    trace_id=trace_id,
                )
            try:
                outcome = deliver(task)
            except Exception:
                outcome = TaskDeliveryAttempt.failed("delivery_callback_failed")
            if isinstance(outcome, bool):
                outcome = (
                    TaskDeliveryAttempt.succeeded()
                    if outcome
                    else TaskDeliveryAttempt.failed("delivery_unsuccessful")
                )
            if not outcome.delivered:
                if outcome.failure_reason != "delivery_unsuccessful":
                    self._service.record_event(
                        task.task_id,
                        TaskEventType.PENDING_DELIVERY,
                        reason=outcome.failure_reason,
                        trace_id=trace_id,
                    )
                continue
            task, _ = self._service.record_event(
                task.task_id,
                TaskEventType.DELIVERING,
                reason="task_delivery_started",
                trace_id=trace_id,
            )
            task, _ = self._service.record_event(
                task.task_id,
                TaskEventType.DELIVERED,
                reason="task_delivered",
                trace_id=trace_id,
            )
            delivered.append(task)
        return delivered
