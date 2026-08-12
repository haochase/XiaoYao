import asyncio
from datetime import UTC, datetime

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.domain.scheduler import TaskScheduler
from companion_gateway.domain.tasks import TaskStatus
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository


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
        confirmation_policy=ConfirmationPolicy.OPTIONAL,
        idempotency_key="scheduler:1",
    )


def test_scheduler_tick_executes_due_tasks_once(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "scheduler.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))
    task, _ = executor.create_and_schedule(task_command(), trace_id="trace-create")
    delivered: list[str] = []
    scheduler = TaskScheduler(
        executor=executor,
        deliver=lambda due_task: delivered.append(due_task.task_id) or True,
        interval_seconds=1.0,
    )

    result = scheduler.tick(now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC))

    assert [item.task_id for item in result] == [task.task_id]
    assert delivered == [task.task_id]
    assert repository.get_task(task.task_id).status is TaskStatus.DELIVERED


def test_scheduler_tick_keeps_failed_delivery_pending(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "scheduler-failed.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))
    task, _ = executor.create_and_schedule(task_command(), trace_id="trace-create")
    scheduler = TaskScheduler(
        executor=executor,
        deliver=lambda due_task: False,
        interval_seconds=1.0,
    )

    scheduler.tick(now=datetime(2026, 8, 7, 12, 1, tzinfo=UTC))

    assert repository.get_task(task.task_id).status is TaskStatus.PENDING_DELIVERY


def test_scheduler_start_and_stop_manage_one_background_loop() -> None:
    async def scenario() -> None:
        calls = 0

        def deliver(_task) -> bool:
            nonlocal calls
            calls += 1
            return True

        class EmptyExecutor:
            def execute_due(self, **kwargs):
                return []

        scheduler = TaskScheduler(
            executor=EmptyExecutor(),
            deliver=deliver,
            interval_seconds=0.01,
        )

        await scheduler.start()
        await scheduler.start()
        await asyncio.sleep(0.03)
        await scheduler.stop()
        await scheduler.stop()

        assert scheduler.is_running is False

    asyncio.run(scenario())
