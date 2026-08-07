from __future__ import annotations

from datetime import UTC, datetime

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
from companion_gateway.domain.executor import TaskExecutor


def task_command() -> TaskCreate:
    return TaskCreate(
        actor_id="voice-user",
        target_device_id="living-room",
        kind=TaskKind.REMINDER,
        schedule=TaskSchedule(
            at="2026-08-07T20:00:00+08:00",
            timezone="Asia/Shanghai",
        ),
        payload=TaskPayload(text="take medicine"),
        confirmation_policy=ConfirmationPolicy.REQUIRED,
        idempotency_key="voice:turn:1",
    )


def test_task_executor_creates_and_schedules_once(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "executor.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))

    first, created = executor.create_and_schedule(
        task_command(),
        trace_id="trace-voice-task",
    )
    second, duplicate = executor.create_and_schedule(
        task_command(),
        trace_id="trace-voice-task-duplicate",
    )

    assert created is True
    assert duplicate is False
    assert first.task_id == second.task_id
    assert first.status is TaskStatus.SCHEDULED
    assert [event.type for event in repository.list_events(first.task_id)] == [
        TaskEventType.CREATED,
        TaskEventType.SCHEDULED,
    ]


def test_task_executor_delivers_due_task_and_records_terminal_event(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "executor-due.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))
    task, _ = executor.create_and_schedule(task_command(), trace_id="trace-create")
    delivered: list[str] = []

    result = executor.execute_due(
        now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
        deliver=lambda due_task: delivered.append(due_task.task_id) or True,
        trace_id="trace-due",
    )

    assert [item.task_id for item in result] == [task.task_id]
    assert delivered == [task.task_id]
    assert repository.get_task(task.task_id).status is TaskStatus.DELIVERED


def test_task_executor_keeps_undeliverable_due_task_pending(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "executor-pending.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))
    task, _ = executor.create_and_schedule(task_command(), trace_id="trace-create")

    result = executor.execute_due(
        now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC),
        deliver=lambda due_task: False,
        trace_id="trace-due",
    )

    assert result == []
    assert repository.get_task(task.task_id).status is TaskStatus.PENDING_DELIVERY
