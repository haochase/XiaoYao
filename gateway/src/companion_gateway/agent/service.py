from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.service import TaskService


class AgentToolNotAllowed(ValueError):
    pass


class AgentToolTimeout(TimeoutError):
    pass


class AgentToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str = Field(min_length=1, max_length=128)
    target_device_id: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class AgentToolPolicy:
    name: str
    timeout_seconds: float
    max_retries: int
    auto_execute: bool


@dataclass(frozen=True)
class AgentToolResult:
    tool: str
    result: dict[str, object]
    auto_executed: bool = False


Clock = Callable[[], datetime]
Monotonic = Callable[[], float]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentToolService:
    """Gateway-owned allowlist for the first two narrow agent tools."""

    _POLICIES: ClassVar[dict[str, AgentToolPolicy]] = {
        "query_task_status": AgentToolPolicy(
            name="query_task_status",
            timeout_seconds=10.0,
            max_retries=0,
            auto_execute=False,
        ),
        "create_reminder": AgentToolPolicy(
            name="create_reminder",
            timeout_seconds=10.0,
            max_retries=0,
            auto_execute=False,
        ),
    }

    def __init__(
        self,
        *,
        task_service: TaskService,
        task_executor: TaskExecutor,
        clock: Clock = _utc_now,
        monotonic: Monotonic | None = None,
    ) -> None:
        self._task_service = task_service
        self._task_executor = task_executor
        self._clock = clock
        self._monotonic = monotonic or time.monotonic

    @classmethod
    def policies(cls) -> dict[str, AgentToolPolicy]:
        return dict(cls._POLICIES)

    def execute(
        self,
        tool_name: str,
        *,
        actor_id: str,
        target_device_id: str,
        arguments: Mapping[str, object],
        trace_id: str,
    ) -> AgentToolResult:
        policy = self._POLICIES.get(tool_name)
        if policy is None:
            raise AgentToolNotAllowed(f"agent tool is not allowed: {tool_name}")
        started = self._monotonic()
        if tool_name == "query_task_status":
            result = self._query_task_status(
                actor_id=actor_id,
                target_device_id=target_device_id,
                arguments=arguments,
            )
        else:
            result = self._create_reminder(
                actor_id=actor_id,
                target_device_id=target_device_id,
                arguments=arguments,
                trace_id=trace_id,
            )
        if self._monotonic() - started > policy.timeout_seconds:
            raise AgentToolTimeout(f"agent tool timed out: {tool_name}")
        return AgentToolResult(
            tool=tool_name,
            result=result,
            auto_executed=policy.auto_execute,
        )

    def create_reminder(
        self,
        *,
        actor_id: str,
        target_device_id: str,
        arguments: Mapping[str, object],
        trace_id: str,
    ) -> AgentToolResult:
        result = self._create_reminder(
            actor_id=actor_id,
            target_device_id=target_device_id,
            arguments=arguments,
            trace_id=trace_id,
            confirmation_policy=ConfirmationPolicy.NONE,
        )
        return AgentToolResult(
            tool="create_reminder",
            result=result,
            auto_executed=False,
        )

    def _query_task_status(
        self,
        *,
        actor_id: str,
        target_device_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("query_task_status requires task_id")
        task = self._task_service.get_task(task_id)
        if (
            task is None
            or task.actor_id != actor_id
            or task.target_device_id != target_device_id
        ):
            return {"found": False, "task_id": task_id}
        return {
            "found": True,
            "task_id": task.task_id,
            "status": task.status.value,
            "kind": task.kind.value,
            "scheduled_at": task.schedule.at.isoformat(),
            "confirmation_policy": task.confirmation_policy.value,
        }

    def _create_reminder(
        self,
        *,
        actor_id: str,
        target_device_id: str,
        arguments: Mapping[str, object],
        trace_id: str,
        confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.REQUIRED,
    ) -> dict[str, object]:
        text = arguments.get("text")
        idempotency_key = arguments.get("idempotency_key")
        raw_schedule = arguments.get("schedule")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("create_reminder requires text")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("create_reminder requires idempotency_key")
        if not isinstance(raw_schedule, Mapping):
            raise ValueError("create_reminder requires schedule")
        try:
            schedule = TaskSchedule.model_validate(dict(raw_schedule))
        except ValidationError as exc:
            raise ValueError("create_reminder schedule is invalid") from exc
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("agent clock must be timezone-aware")
        if schedule.at.astimezone(UTC) <= current.astimezone(UTC):
            raise ValueError("create_reminder schedule must be in the future")
        command = TaskCreate(
            actor_id=actor_id,
            target_device_id=target_device_id,
            kind=TaskKind.REMINDER,
            schedule=schedule,
            payload=TaskPayload(text=text),
            confirmation_policy=confirmation_policy,
            idempotency_key=idempotency_key,
        )
        task, created = self._task_executor.create_and_schedule(
            command,
            trace_id=trace_id,
        )
        return {
            "task_id": task.task_id,
            "created": created,
            "status": task.status.value,
            "confirmation_policy": task.confirmation_policy.value,
            "requires_confirmation": task.status.value == "awaiting_confirmation",
            "auto_executed": False,
        }
