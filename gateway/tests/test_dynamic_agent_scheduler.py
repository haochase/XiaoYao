from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from companion_gateway.agent.scheduler import DynamicAgentScheduler
from companion_gateway.domain.agents import (
    AgentChannel,
    AgentExecution,
    AgentExecutionStatus,
    AgentKind,
    AgentMemoryPolicy,
    AgentSpec,
    AgentToolName,
    AgentTrigger,
    TriggerKind,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MONDAY_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
SUNDAY_NOW = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)


def build_agent(
    *,
    agent_id: str,
    trigger: AgentTrigger,
    enabled: bool = True,
) -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        owner_id="family-1",
        name=agent_id,
        kind=AgentKind.REMINDER,
        enabled=enabled,
        trigger=trigger,
        channels=(AgentChannel.FEISHU,),
        allowed_tools=(AgentToolName.SEND_FEISHU,),
        prompt="请发送提醒。",
        memory_policy=AgentMemoryPolicy.NONE,
        max_turns=1,
        config={"message": "喝水"},
    )


class SchedulingRepository:
    def __init__(self, *agents: AgentSpec) -> None:
        self._agents = tuple(agents)
        self._executions: list[AgentExecution] = []
        self.list_owners: list[str] = []

    def list_agents(self, *, owner_id: str) -> list[AgentSpec]:
        self.list_owners.append(owner_id)
        return [agent for agent in self._agents if agent.owner_id == owner_id]

    def list_executions(
        self,
        agent_id: str,
        *,
        owner_id: str,
    ) -> list[AgentExecution]:
        return [
            execution
            for execution in self._executions
            if execution.agent_id == agent_id and owner_id == "family-1"
        ]

    def record(self, execution: AgentExecution) -> None:
        self._executions.append(execution)

    def record_execution(
        self,
        execution: AgentExecution,
        *,
        owner_id: str,
    ) -> AgentExecution:
        assert owner_id == "family-1"
        self.record(execution)
        return execution


class RecordingRuntime:
    def __init__(self, repository: SchedulingRepository) -> None:
        self._repository = repository
        self.calls: list[tuple[str, str, str, datetime]] = []

    def run(
        self,
        agent_id: str,
        *,
        owner_id: str,
        trigger_id: str,
        now: datetime,
    ) -> AgentExecution:
        self.calls.append((agent_id, owner_id, trigger_id, now))
        execution = AgentExecution(
            execution_id=f"execution-{len(self.calls)}",
            agent_id=agent_id,
            trigger_id=trigger_id,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            output_text="已执行",
        )
        self._repository.record(execution)
        return execution


def test_scheduler_runs_due_once_daily_and_weekday_agents_once_per_trigger_key() -> None:
    once_at = MONDAY_NOW - timedelta(minutes=1)
    repository = SchedulingRepository(
        build_agent(
            agent_id="agent-once",
            trigger=AgentTrigger(
                kind=TriggerKind.ONCE,
                timezone="Asia/Shanghai",
                at=once_at,
            ),
        ),
        build_agent(
            agent_id="agent-daily",
            trigger=AgentTrigger(
                kind=TriggerKind.DAILY,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
        build_agent(
            agent_id="agent-weekday",
            trigger=AgentTrigger(
                kind=TriggerKind.WEEKDAYS,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
        build_agent(
            agent_id="agent-manual",
            trigger=AgentTrigger(kind=TriggerKind.MANUAL),
        ),
        build_agent(
            agent_id="agent-disabled",
            enabled=False,
            trigger=AgentTrigger(
                kind=TriggerKind.DAILY,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
    )
    runtime = RecordingRuntime(repository)
    scheduler = DynamicAgentScheduler(
        repository=repository,
        runtime=runtime,
        owner_id="family-1",
        interval_seconds=60,
    )

    first = scheduler.tick(now=MONDAY_NOW)
    second = scheduler.tick(now=MONDAY_NOW + timedelta(minutes=1))

    assert [execution.agent_id for execution in first] == [
        "agent-daily",
        "agent-once",
        "agent-weekday",
    ]
    assert second == ()
    assert [call[0] for call in runtime.calls] == [
        "agent-daily",
        "agent-once",
        "agent-weekday",
    ]
    assert all(call[1] == "family-1" for call in runtime.calls)
    assert all(call[2].startswith(f"{call[0]}+") for call in runtime.calls)
    assert repository.list_owners == ["family-1", "family-1"]


def test_scheduler_skips_weekday_agents_on_a_sunday_and_manual_agents_always() -> None:
    repository = SchedulingRepository(
        build_agent(
            agent_id="agent-weekday",
            trigger=AgentTrigger(
                kind=TriggerKind.WEEKDAYS,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
        build_agent(
            agent_id="agent-manual",
            trigger=AgentTrigger(kind=TriggerKind.MANUAL),
        ),
    )
    runtime = RecordingRuntime(repository)
    scheduler = DynamicAgentScheduler(
        repository=repository,
        runtime=runtime,
        owner_id="family-1",
        interval_seconds=60,
    )

    assert SUNDAY_NOW.astimezone(SHANGHAI).weekday() == 6
    assert scheduler.tick(now=SUNDAY_NOW) == ()
    assert runtime.calls == []


def test_scheduler_start_and_stop_are_idempotent() -> None:
    repository = SchedulingRepository()
    runtime = RecordingRuntime(repository)
    scheduler = DynamicAgentScheduler(
        repository=repository,
        runtime=runtime,
        owner_id="family-1",
        interval_seconds=3_600,
        clock=lambda: MONDAY_NOW,
    )

    async def exercise() -> None:
        await scheduler.start()
        assert scheduler.is_running is True
        await scheduler.start()
        await scheduler.stop()
        assert scheduler.is_running is False
        await scheduler.stop()

    asyncio.run(exercise())


def test_scheduler_records_failure_and_continues_after_one_agent_raises() -> None:
    repository = SchedulingRepository(
        build_agent(
            agent_id="agent-a-fails",
            trigger=AgentTrigger(
                kind=TriggerKind.DAILY,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
        build_agent(
            agent_id="agent-b-runs",
            trigger=AgentTrigger(
                kind=TriggerKind.DAILY,
                timezone="Asia/Shanghai",
                local_time=time(7, 30),
            ),
        ),
    )

    class PartiallyFailingRuntime(RecordingRuntime):
        def run(self, agent_id: str, **kwargs) -> AgentExecution:
            if agent_id == "agent-a-fails":
                raise RuntimeError("secret upstream detail")
            return super().run(agent_id, **kwargs)

    runtime = PartiallyFailingRuntime(repository)
    scheduler = DynamicAgentScheduler(
        repository=repository,
        runtime=runtime,
        owner_id="family-1",
        interval_seconds=60,
    )

    executions = scheduler.tick(now=MONDAY_NOW)

    assert [execution.agent_id for execution in executions] == [
        "agent-a-fails",
        "agent-b-runs",
    ]
    assert executions[0].status is AgentExecutionStatus.FAILED
    assert "secret upstream detail" not in executions[0].error
    assert executions[1].status is AgentExecutionStatus.SUCCEEDED
