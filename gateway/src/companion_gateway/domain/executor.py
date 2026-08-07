from collections.abc import Callable
from datetime import datetime

from companion_gateway.domain.models import TaskCreate, TaskRecord
from companion_gateway.domain.tasks import TaskEventType, TaskStatus
from companion_gateway.service import TaskService


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
        deliver: Callable[[TaskRecord], bool],
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
                is_delivered = deliver(task)
            except Exception:
                is_delivered = False
            if not is_delivered:
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
