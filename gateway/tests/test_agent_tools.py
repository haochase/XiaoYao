from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.agent.service import (
    AgentToolNotAllowed,
    AgentToolService,
    AgentToolTimeout,
)
from companion_gateway.domain.tasks import TaskStatus
from companion_gateway.domain.models import TaskCreate
from companion_gateway.service import TaskService
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def create_service(tmp_path, *, monotonic=None):
    repository = SQLiteTaskRepository(tmp_path / "agent.db")
    repository.initialize()
    task_service = TaskService(repository, clock=lambda: NOW)
    executor = TaskExecutor(task_service)
    return AgentToolService(
        task_service=task_service,
        task_executor=executor,
        clock=lambda: NOW,
        monotonic=monotonic,
    ), repository


def reminder_arguments(*, key: str = "agent-reminder-1") -> dict[str, object]:
    return {
        "schedule": {
            "at": (NOW + timedelta(minutes=5)).isoformat(),
            "timezone": "Asia/Shanghai",
        },
        "text": "请提醒我喝水",
        "idempotency_key": key,
    }


def test_agent_exposes_only_two_non_auto_executing_tools() -> None:
    policies = AgentToolService.policies()

    assert set(policies) == {"query_task_status", "create_reminder"}
    assert all(policy.auto_execute is False for policy in policies.values())
    assert policies["query_task_status"].timeout_seconds == 10
    assert policies["create_reminder"].max_retries == 0


def test_query_task_status_is_subject_and_device_scoped(tmp_path) -> None:
    service, repository = create_service(tmp_path)
    task_service = TaskService(repository, clock=lambda: NOW)
    executor = TaskExecutor(task_service)
    created, _ = executor.create_and_schedule(
        TaskCreate.model_validate(
            {
                "actor_id": "family-1",
                "target_device_id": "living-room",
                "kind": "reminder",
                "schedule": {
                    "at": (NOW + timedelta(minutes=5)).isoformat(),
                    "timezone": "Asia/Shanghai",
                },
                "payload": {"text": "drink water"},
                "confirmation_policy": "required",
                "idempotency_key": "seed-task-1",
            }
        ),
        trace_id="trace-seed",
    )

    own = service.execute(
        "query_task_status",
        actor_id="family-1",
        target_device_id="living-room",
        arguments={"task_id": created.task_id},
        trace_id="trace-query-own",
    )
    other = service.execute(
        "query_task_status",
        actor_id="family-2",
        target_device_id="living-room",
        arguments={"task_id": created.task_id},
        trace_id="trace-query-other",
    )

    assert own.result["found"] is True
    assert own.result["status"] == TaskStatus.AWAITING_CONFIRMATION.value
    assert other.result == {"found": False, "task_id": created.task_id}


def test_create_reminder_is_required_and_idempotent(tmp_path) -> None:
    service, repository = create_service(tmp_path)

    first = service.execute(
        "create_reminder",
        actor_id="family-1",
        target_device_id="living-room",
        arguments=reminder_arguments(),
        trace_id="trace-create-1",
    )
    second = service.execute(
        "create_reminder",
        actor_id="family-1",
        target_device_id="living-room",
        arguments=reminder_arguments(),
        trace_id="trace-create-2",
    )

    assert first.result["status"] == TaskStatus.AWAITING_CONFIRMATION.value
    assert first.result["confirmation_policy"] == "required"
    assert first.result["requires_confirmation"] is True
    assert first.result["auto_executed"] is False
    assert first.result["created"] is True
    assert second.result["task_id"] == first.result["task_id"]
    assert second.result["created"] is False
    assert repository.get_task(first.result["task_id"]).status is TaskStatus.AWAITING_CONFIRMATION


def test_unknown_tool_and_past_reminder_are_rejected(tmp_path) -> None:
    service, _repository = create_service(tmp_path)

    with pytest.raises(AgentToolNotAllowed):
        service.execute(
            "send_feishu",
            actor_id="family-1",
            target_device_id="living-room",
            arguments={},
            trace_id="trace-forbidden",
        )

    with pytest.raises(ValueError, match="future"):
        service.execute(
            "create_reminder",
            actor_id="family-1",
            target_device_id="living-room",
            arguments={
                **reminder_arguments(),
                "schedule": {
                    "at": (NOW - timedelta(minutes=1)).isoformat(),
                    "timezone": "Asia/Shanghai",
                },
            },
            trace_id="trace-past",
        )


def test_tool_timeout_is_reported_without_retrying_mutating_tool(tmp_path) -> None:
    values = iter([0.0, 11.0, 11.0, 11.0])
    service, _repository = create_service(tmp_path, monotonic=lambda: next(values))

    with pytest.raises(AgentToolTimeout):
        service.execute(
            "create_reminder",
            actor_id="family-1",
            target_device_id="living-room",
            arguments=reminder_arguments(),
            trace_id="trace-timeout",
        )

    # A retry with the same idempotency key returns the existing proposal.
    retry = service.execute(
        "create_reminder",
        actor_id="family-1",
        target_device_id="living-room",
        arguments=reminder_arguments(),
        trace_id="trace-timeout-retry",
    )
    assert retry.result["created"] is False
