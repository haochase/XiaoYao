from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_gateway.agent.runtime import AgentRuntime
from companion_gateway.agent.templates.summary import DailySummaryBuilder, DailySummaryFacts
from companion_gateway.agent.tools.weather import WeatherAdvice
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


NOW = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)


def build_agent(
    *,
    agent_id: str,
    kind: AgentKind,
    channels: tuple[AgentChannel, ...] = (AgentChannel.FEISHU,),
    config: dict[str, object] | None = None,
    prompt: str = "请开始本次练习。",
    enabled: bool = True,
    allowed_tools: tuple[AgentToolName, ...] = (
        AgentToolName.WEATHER_FORECAST,
        AgentToolName.SEND_FEISHU,
        AgentToolName.SPEAK_ESP32,
        AgentToolName.DAILY_SUMMARY,
    ),
) -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        owner_id="family-1",
        name=f"{kind.value}-agent",
        kind=kind,
        enabled=enabled,
        trigger=AgentTrigger(kind=TriggerKind.MANUAL),
        channels=channels,
        allowed_tools=allowed_tools,
        prompt=prompt,
        memory_policy=AgentMemoryPolicy.NONE,
        max_turns=1,
        config=config or {},
    )


class RecordingRepository:
    def __init__(self, *agents: AgentSpec) -> None:
        self._agents = {agent.agent_id: agent for agent in agents}
        self.records: list[tuple[AgentExecution, str]] = []

    def get_agent(self, agent_id: str, *, owner_id: str) -> AgentSpec | None:
        agent = self._agents.get(agent_id)
        if agent is None or agent.owner_id != owner_id:
            return None
        return agent

    def record_execution(
        self,
        execution: AgentExecution,
        *,
        owner_id: str,
    ) -> AgentExecution:
        if self.get_agent(execution.agent_id, owner_id=owner_id) is None:
            raise PermissionError("agent execution owner mismatch")
        self.records.append((execution, owner_id))
        return execution


class StaticWeatherTool:
    def __init__(self, advice: WeatherAdvice) -> None:
        self.advice = advice
        self.calls: list[tuple[str, datetime]] = []

    def advise(self, city: str, *, now: datetime) -> WeatherAdvice:
        self.calls.append((city, now))
        return self.advice


class FailingWeatherTool:
    def advise(self, city: str, *, now: datetime) -> WeatherAdvice:
        raise RuntimeError("api-key=top-secret")


def weather_advice() -> WeatherAdvice:
    return WeatherAdvice(
        city="北京",
        observed_at=NOW,
        temperature_c=12.0,
        apparent_temperature_c=10.0,
        precipitation_probability=60,
        weather_code=61,
        wind_speed_kmh=15.0,
        clothing=("外套",),
        carry_umbrella=True,
        source="live",
    )


def test_runtime_persists_weather_fact_before_both_channel_deliveries() -> None:
    agent = build_agent(
        agent_id="agent-weather",
        kind=AgentKind.WEATHER,
        channels=(AgentChannel.FEISHU, AgentChannel.ESP32),
        config={"city": "北京"},
    )
    repository = RecordingRepository(agent)
    weather_tool = StaticWeatherTool(weather_advice())
    delivered: list[tuple[str, str]] = []

    def send_feishu(text: str) -> bool:
        assert repository.records[-1][0].status is AgentExecutionStatus.SUCCEEDED
        delivered.append(("feishu", text))
        return True

    def speak_esp32(text: str) -> bool:
        assert repository.records[-1][0].status is AgentExecutionStatus.SUCCEEDED
        delivered.append(("esp32", text))
        return True

    runtime = AgentRuntime(
        repository=repository,
        weather_tool=weather_tool,
        send_feishu=send_feishu,
        speak_esp32=speak_esp32,
        summary_builder=lambda owner_id, now: "unused",
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id="agent-weather+manual-1",
        now=NOW,
    )

    assert execution.status is AgentExecutionStatus.SUCCEEDED
    assert "北京" in execution.output_text
    assert "外套" in execution.output_text
    assert "带伞" in execution.output_text
    assert weather_tool.calls == [("北京", NOW)]
    assert delivered == [("feishu", execution.output_text), ("esp32", execution.output_text)]
    assert [record.status for record, _owner in repository.records] == [
        AgentExecutionStatus.STARTED,
        AgentExecutionStatus.SUCCEEDED,
    ]
    assert {record.execution_id for record, _owner in repository.records} == {
        execution.execution_id
    }
    assert {owner_id for _record, owner_id in repository.records} == {agent.owner_id}


def test_runtime_keeps_same_execution_record_when_channels_return_false_or_raise() -> None:
    agent = build_agent(
        agent_id="agent-reminder",
        kind=AgentKind.REMINDER,
        channels=(AgentChannel.FEISHU, AgentChannel.ESP32),
        config={"message": "请记得喝水"},
    )
    repository = RecordingRepository(agent)
    delivered: list[str] = []

    def send_feishu(text: str) -> bool:
        delivered.append(f"feishu:{text}")
        return False

    def speak_esp32(text: str) -> bool:
        delivered.append(f"esp32:{text}")
        raise RuntimeError("offline")

    runtime = AgentRuntime(
        repository=repository,
        weather_tool=StaticWeatherTool(weather_advice()),
        send_feishu=send_feishu,
        speak_esp32=speak_esp32,
        summary_builder=lambda owner_id, now: "unused",
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id="agent-reminder+manual-1",
        now=NOW,
    )

    assert execution.status is AgentExecutionStatus.FAILED
    assert execution.output_text == "请记得喝水"
    assert execution.error is not None
    assert "Feishu" in execution.error
    assert "ESP32" in execution.error
    assert delivered == ["feishu:请记得喝水", "esp32:请记得喝水"]
    assert [record.status for record, _owner in repository.records] == [
        AgentExecutionStatus.STARTED,
        AgentExecutionStatus.SUCCEEDED,
        AgentExecutionStatus.FAILED,
    ]
    assert {record.execution_id for record, _owner in repository.records} == {
        execution.execution_id
    }


@pytest.mark.parametrize(
    ("kind", "config", "prompt", "expected"),
    [
        (AgentKind.REMINDER, {"message": "记得喝水"}, "unused", "记得喝水"),
        (AgentKind.MEDICATION, {"message": "记得服药"}, "unused", "记得服药"),
        (AgentKind.COMPANION, {}, "请开始陪伴对话。", "请开始陪伴对话。"),
        (AgentKind.ENGLISH, {}, "请开始英语练习。", "请开始英语练习。"),
    ],
)
def test_runtime_uses_deterministic_message_or_initial_prompt_by_agent_kind(
    kind: AgentKind,
    config: dict[str, object],
    prompt: str,
    expected: str,
) -> None:
    agent = build_agent(
        agent_id=f"agent-{kind.value}",
        kind=kind,
        config=config,
        prompt=prompt,
    )
    repository = RecordingRepository(agent)
    sent: list[str] = []
    runtime = AgentRuntime(
        repository=repository,
        weather_tool=StaticWeatherTool(weather_advice()),
        send_feishu=lambda text: sent.append(text) is None,
        speak_esp32=lambda text: True,
        summary_builder=lambda owner_id, now: "unused",
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id=f"{agent.agent_id}+manual-1",
        now=NOW,
    )

    assert execution.status is AgentExecutionStatus.SUCCEEDED
    assert execution.output_text == expected
    assert sent == [expected]


def test_daily_summary_builder_reads_injected_facts_and_runtime_sends_result() -> None:
    calls: list[tuple[str, datetime]] = []
    builder = DailySummaryBuilder(
        facts_provider=lambda owner_id, now: (
            calls.append((owner_id, now))
            or DailySummaryFacts(
                reminders=("喝水提醒已完成",),
                medications=("服药提醒待确认",),
                agent_executions=("天气助手执行成功",),
            )
        )
    )
    agent = build_agent(agent_id="agent-summary", kind=AgentKind.DAILY_SUMMARY)
    repository = RecordingRepository(agent)
    sent: list[str] = []
    runtime = AgentRuntime(
        repository=repository,
        weather_tool=StaticWeatherTool(weather_advice()),
        send_feishu=lambda text: sent.append(text) is None,
        speak_esp32=lambda text: True,
        summary_builder=builder,
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id="agent-summary+manual-1",
        now=NOW,
    )

    assert calls == [(agent.owner_id, NOW)]
    assert "喝水提醒已完成" in execution.output_text
    assert "服药提醒待确认" in execution.output_text
    assert "天气助手执行成功" in execution.output_text
    assert sent == [execution.output_text]


def test_runtime_rejects_another_owner_before_creating_execution_state() -> None:
    agent = build_agent(agent_id="agent-private", kind=AgentKind.COMPANION)
    repository = RecordingRepository(agent)
    runtime = AgentRuntime(
        repository=repository,
        weather_tool=StaticWeatherTool(weather_advice()),
        send_feishu=lambda text: True,
        speak_esp32=lambda text: True,
        summary_builder=lambda owner_id, now: "unused",
    )

    with pytest.raises(KeyError):
        runtime.run(
            agent.agent_id,
            owner_id="family-2",
            trigger_id="agent-private+manual-1",
            now=NOW,
        )

    assert repository.records == []


def test_runtime_never_invokes_a_tool_outside_the_agent_allowlist() -> None:
    agent = build_agent(
        agent_id="agent-restricted-weather",
        kind=AgentKind.WEATHER,
        config={"city": "Beijing"},
        allowed_tools=(AgentToolName.SEND_FEISHU,),
    )
    repository = RecordingRepository(agent)
    weather_tool = StaticWeatherTool(weather_advice())
    sent: list[str] = []
    runtime = AgentRuntime(
        repository=repository,
        weather_tool=weather_tool,
        send_feishu=lambda text: sent.append(text) is None,
        speak_esp32=lambda text: True,
        summary_builder=lambda owner_id, now: "unused",
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id="agent-restricted-weather+manual-1",
        now=NOW,
    )

    assert execution.status is AgentExecutionStatus.FAILED
    assert execution.error == "agent tool not allowed: weather_forecast"
    assert weather_tool.calls == []
    assert sent == []


def test_runtime_redacts_external_exception_detail_from_persisted_error() -> None:
    agent = build_agent(
        agent_id="agent-failing-weather",
        kind=AgentKind.WEATHER,
        config={"city": "Beijing"},
    )
    repository = RecordingRepository(agent)
    runtime = AgentRuntime(
        repository=repository,
        weather_tool=FailingWeatherTool(),
        send_feishu=lambda text: True,
        speak_esp32=lambda text: True,
        summary_builder=lambda owner_id, now: "unused",
    )

    execution = runtime.run(
        agent.agent_id,
        owner_id=agent.owner_id,
        trigger_id="agent-failing-weather+manual-1",
        now=NOW,
    )

    assert execution.status is AgentExecutionStatus.FAILED
    assert execution.error == "agent execution failed: RuntimeError"
    assert "top-secret" not in execution.error
    assert [record.status for record, _owner in repository.records] == [
        AgentExecutionStatus.STARTED,
        AgentExecutionStatus.FAILED,
    ]
