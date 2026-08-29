from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from companion_gateway.agent.runtime import AgentRuntime, execution_id_for
from companion_gateway.domain.agents import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRepository,
    AgentSpec,
    TriggerKind,
)


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DynamicAgentScheduler:
    """Single-owner loop that derives idempotent due trigger keys."""

    def __init__(
        self,
        *,
        repository: AgentRepository,
        runtime: AgentRuntime,
        owner_id: str,
        interval_seconds: float,
        clock: Clock = _utc_now,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("agent scheduler owner_id must not be empty")
        if interval_seconds <= 0:
            raise ValueError("agent scheduler interval_seconds must be positive")
        self._repository = repository
        self._runtime = runtime
        self._owner_id = owner_id
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def tick(self, *, now: datetime | None = None) -> tuple:
        current = now or self._clock()
        _require_aware(current)
        executions = []
        agents = sorted(
            self._repository.list_agents(owner_id=self._owner_id),
            key=lambda agent: agent.agent_id,
        )
        for agent in agents:
            scheduled_at = _due_at(agent, now=current)
            if not agent.enabled or scheduled_at is None:
                continue
            trigger_id = _trigger_id(agent.agent_id, scheduled_at)
            existing = self._repository.list_executions(
                agent.agent_id,
                owner_id=self._owner_id,
            )
            if any(execution.trigger_id == trigger_id for execution in existing):
                continue
            try:
                execution = self._runtime.run(
                    agent.agent_id,
                    owner_id=self._owner_id,
                    trigger_id=trigger_id,
                    now=current,
                )
            except Exception as exc:
                execution = AgentExecution(
                    execution_id=execution_id_for(
                        agent_id=agent.agent_id,
                        trigger_id=trigger_id,
                    ),
                    agent_id=agent.agent_id,
                    trigger_id=trigger_id,
                    status=AgentExecutionStatus.FAILED,
                    started_at=current,
                    completed_at=current,
                    error=f"agent scheduler failed: {type(exc).__name__}",
                )
                try:
                    self._repository.record_execution(
                        execution,
                        owner_id=self._owner_id,
                    )
                except Exception:
                    pass
            executions.append(execution)
        return tuple(executions)

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._stop_event))

    async def stop(self) -> None:
        task = self._task
        stop_event = self._stop_event
        if task is None or stop_event is None:
            return
        stop_event.set()
        await task
        self._task = None
        self._stop_event = None

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.to_thread(self.tick)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                continue


def _due_at(agent: AgentSpec, *, now: datetime) -> datetime | None:
    trigger = agent.trigger
    if trigger.kind is TriggerKind.MANUAL:
        return None
    if trigger.kind is TriggerKind.ONCE:
        if trigger.at is None or now < trigger.at:
            return None
        return trigger.at
    if trigger.timezone is None or trigger.local_time is None:
        return None
    timezone = ZoneInfo(trigger.timezone)
    local_now = now.astimezone(timezone)
    if trigger.kind is TriggerKind.WEEKDAYS and local_now.weekday() >= 5:
        return None
    scheduled_at = datetime.combine(local_now.date(), trigger.local_time).replace(
        tzinfo=timezone
    )
    if local_now < scheduled_at:
        return None
    return scheduled_at


def _trigger_id(agent_id: str, scheduled_at: datetime) -> str:
    raw = f"{agent_id}+{scheduled_at.isoformat()}"
    if len(raw) <= 128:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()
    return f"{agent_id[:60]}+{digest}"


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("agent scheduler now must be timezone-aware")
