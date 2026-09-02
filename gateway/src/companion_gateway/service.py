from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from companion_gateway.domain.medication import (
    MedicationOccurrence,
    MedicationPlan,
    MedicationPlanCreate,
)
from companion_gateway.domain.models import TaskCreate, TaskEvent, TaskKind, TaskRecord
from companion_gateway.domain.tasks import TaskEventType
from companion_gateway.storage.sqlite import SQLiteTaskRepository


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class TaskService:
    def __init__(
        self,
        repository: SQLiteTaskRepository,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = _new_id,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory

    def create_task(
        self,
        command: TaskCreate,
        *,
        trace_id: str,
    ) -> tuple[TaskRecord, bool]:
        return self._repository.create_task(
            command,
            task_id=self._id_factory("tsk"),
            event_id=self._id_factory("evt"),
            trace_id=trace_id,
            occurred_at=self._clock(),
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._repository.get_task(task_id)

    def get_task_by_idempotency_key(
        self, idempotency_key: str
    ) -> TaskRecord | None:
        return self._repository.get_task_by_idempotency_key(idempotency_key)

    def get_latest_reminder(
        self,
        *,
        actor_id: str,
        target_device_id: str,
    ) -> TaskRecord | None:
        return self._repository.get_latest_task(
            actor_id=actor_id,
            target_device_id=target_device_id,
            kind=TaskKind.REMINDER,
        )

    def get_events(self, task_id: str) -> list[TaskEvent]:
        return self._repository.list_events(task_id)

    def list_due_tasks(self, *, now: datetime) -> list[TaskRecord]:
        return self._repository.list_due_tasks(now=now)

    def create_medication_plan(
        self,
        plan: MedicationPlanCreate,
        *,
        trace_id: str,
    ) -> tuple[MedicationPlan, bool]:
        return self._repository.create_medication_plan(
            plan,
            plan_id=self._id_factory("medplan"),
            occurred_at=self._clock(),
        )

    def get_medication_plan(self, plan_id: str) -> MedicationPlan | None:
        return self._repository.get_medication_plan(plan_id)

    def list_medication_plans(
        self, *, enabled: bool | None = None
    ) -> list[MedicationPlan]:
        return self._repository.list_medication_plans(enabled=enabled)

    def disable_medication_plan(
        self, plan_id: str, *, occurred_at: datetime
    ) -> MedicationPlan:
        return self._repository.disable_medication_plan(
            plan_id,
            occurred_at=occurred_at,
        )

    def list_medication_occurrences(self) -> list[MedicationOccurrence]:
        return self._repository.list_medication_occurrences()

    def record_event(
        self,
        task_id: str,
        event_type: TaskEventType,
        *,
        reason: str | None,
        trace_id: str,
    ) -> tuple[TaskRecord, TaskEvent]:
        event = self._repository.append_event(
            task_id,
            event_type,
            event_id=self._id_factory("evt"),
            trace_id=trace_id,
            reason=reason,
            occurred_at=self._clock(),
        )
        task = self._repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task, event
