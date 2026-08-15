from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.domain.models import TaskCreate, TaskKind, TaskPayload
from companion_gateway.domain.tasks import (
    InvalidTaskTransition,
    TaskEventType,
    TaskStatus,
)
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def task_command(idempotency_key: str = "client:message-1") -> TaskCreate:
    return TaskCreate.model_validate(
        {
            "actor_id": "family-1",
            "target_device_id": "living-room",
            "kind": "reminder",
            "schedule": {
                "at": "2026-08-05T20:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
            "payload": {"text": "take medicine"},
            "confirmation_policy": "required",
            "idempotency_key": idempotency_key,
        }
    )


def create_once(
    repository: SQLiteTaskRepository,
    command: TaskCreate | None = None,
    *,
    suffix: str = "1",
) -> tuple[object, bool]:
    return repository.create_task(
        command or task_command(),
        task_id=f"tsk-{suffix}",
        event_id=f"evt-{suffix}",
        trace_id=f"trc-{suffix}",
        occurred_at=NOW,
    )


def test_create_task_is_idempotent(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "tasks.db")
    repository.initialize()

    first, first_created = create_once(repository, suffix="1")
    second, second_created = create_once(repository, suffix="2")

    assert first_created is True
    assert second_created is False
    assert second.task_id == first.task_id == "tsk-1"
    assert [event.type for event in repository.list_events(first.task_id)] == [
        TaskEventType.CREATED
    ]


def test_invalid_transition_rolls_back_event_and_status(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "tasks.db")
    repository.initialize()
    task, _ = create_once(repository)

    with pytest.raises(InvalidTaskTransition):
        repository.append_event(
            task.task_id,
            TaskEventType.ACKNOWLEDGED,
            event_id="evt-invalid",
            trace_id="trc-invalid",
            reason="too_early",
            occurred_at=NOW + timedelta(seconds=1),
        )

    stored = repository.get_task(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.CREATED
    assert len(repository.list_events(task.task_id)) == 1


def test_valid_event_sequence_updates_task_and_preserves_events(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "tasks.db")
    repository.initialize()
    task, _ = create_once(repository)
    sequence = [
        TaskEventType.SCHEDULED,
        TaskEventType.DUE,
        TaskEventType.DELIVERING,
        TaskEventType.DELIVERED,
        TaskEventType.ACKNOWLEDGED,
    ]

    for index, event_type in enumerate(sequence, start=2):
        repository.append_event(
            task.task_id,
            event_type,
            event_id=f"evt-{index}",
            trace_id="trc-1",
            reason=f"test_{event_type.value}",
            occurred_at=NOW + timedelta(seconds=index),
        )

    stored = repository.get_task(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.ACKNOWLEDGED
    assert [event.type for event in repository.list_events(task.task_id)] == [
        TaskEventType.CREATED,
        *sequence,
    ]


def test_repository_recovers_after_new_instance_opens_same_database(tmp_path) -> None:
    database_path = tmp_path / "tasks.db"
    first_repository = SQLiteTaskRepository(database_path)
    first_repository.initialize()
    task, _ = create_once(first_repository)

    reopened_repository = SQLiteTaskRepository(database_path)
    reopened_repository.initialize()
    recovered = reopened_repository.get_task(task.task_id)

    assert recovered == task
    assert reopened_repository.check() is True
    assert len(reopened_repository.list_events(task.task_id)) == 1


def test_missing_task_returns_none_and_no_events(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "tasks.db")
    repository.initialize()

    assert repository.get_task("missing") is None
    assert repository.list_events("missing") == []


def test_latest_task_is_scoped_by_actor_device_and_kind(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "tasks.db")
    repository.initialize()
    first, _ = create_once(repository, suffix="first")
    latest_command = task_command("client:latest").model_copy(
        update={"payload": TaskPayload(text="latest reminder")}
    )
    latest, _ = repository.create_task(
        latest_command,
        task_id="tsk-latest",
        event_id="evt-latest",
        trace_id="trc-latest",
        occurred_at=NOW + timedelta(minutes=1),
    )
    other_device = task_command("client:other-device").model_copy(
        update={"target_device_id": "bedroom"}
    )
    repository.create_task(
        other_device,
        task_id="tsk-other-device",
        event_id="evt-other-device",
        trace_id="trc-other-device",
        occurred_at=NOW + timedelta(minutes=2),
    )

    assert repository.get_latest_task(
        actor_id="family-1",
        target_device_id="living-room",
        kind=TaskKind.REMINDER,
    ) == latest
    assert repository.get_latest_task(
        actor_id="family-1",
        target_device_id="missing-device",
        kind=TaskKind.REMINDER,
    ) is None
    assert first.task_id == "tsk-first"
