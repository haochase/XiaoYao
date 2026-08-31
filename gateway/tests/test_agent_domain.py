from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from companion_gateway.domain.agents import (
    AgentChannel,
    AgentDraft,
    AgentExecution,
    AgentExecutionStatus,
    AgentKind,
    AgentMemoryPolicy,
    AgentSpec,
    AgentToolName,
    AgentTrigger,
    TriggerKind,
)


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def build_spec(**overrides: object) -> AgentSpec:
    data: dict[str, object] = {
        "agent_id": "agent-weather-1",
        "owner_id": "family-1",
        "name": "出门穿衣提醒",
        "kind": AgentKind.WEATHER,
        "enabled": True,
        "trigger": {
            "kind": "daily",
            "timezone": "Asia/Shanghai",
            "local_time": "07:30",
        },
        "channels": (AgentChannel.FEISHU, AgentChannel.ESP32),
        "allowed_tools": (
            AgentToolName.WEATHER_FORECAST,
            AgentToolName.SPEAK_ESP32,
        ),
        "prompt": "根据天气给出简短的穿衣建议。",
        "memory_policy": AgentMemoryPolicy.READ_CONFIRMED,
        "max_turns": 3,
        "config": {"city": "Shanghai"},
    }
    data.update(overrides)
    return AgentSpec.model_validate(data)


def test_agent_spec_accepts_the_declared_registry_contract() -> None:
    spec = build_spec()

    assert spec.kind is AgentKind.WEATHER
    assert spec.trigger.kind is TriggerKind.DAILY
    assert spec.trigger.local_time == time(7, 30)
    assert spec.channels == (AgentChannel.FEISHU, AgentChannel.ESP32)
    assert spec.allowed_tools == (
        AgentToolName.WEATHER_FORECAST,
        AgentToolName.SPEAK_ESP32,
    )
    assert spec.memory_policy is AgentMemoryPolicy.READ_CONFIRMED


@pytest.mark.parametrize(
    "trigger",
    [
        {
            "kind": "manual",
            "at": "2026-08-29T12:00:00+00:00",
        },
        {
            "kind": "manual",
            "local_time": "07:30",
        },
        {
            "kind": "manual",
            "timezone": "Asia/Shanghai",
        },
        {
            "kind": "once",
            "timezone": "Asia/Shanghai",
            "at": datetime(2026, 8, 29, 12, 0),
        },
        {
            "kind": "once",
            "timezone": "Mars/Olympus",
            "at": "2026-08-29T12:00:00+00:00",
        },
        {
            "kind": "daily",
            "timezone": "Asia/Shanghai",
        },
        {
            "kind": "weekdays",
            "timezone": "Asia/Shanghai",
            "at": "2026-08-29T12:00:00+00:00",
            "local_time": "07:30",
        },
        {
            "kind": "daily",
            "timezone": "Asia/Shanghai",
            "local_time": "25:00",
        },
    ],
)
def test_agent_trigger_rejects_invalid_time_combinations(
    trigger: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AgentTrigger.model_validate(trigger)


def test_agent_trigger_accepts_once_with_an_aware_time_and_iana_timezone() -> None:
    trigger = AgentTrigger(
        kind=TriggerKind.ONCE,
        timezone="Asia/Shanghai",
        at=NOW,
    )

    assert trigger.at == NOW
    assert trigger.local_time is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channels", ("wechat",)),
        ("allowed_tools", ("delete_everything",)),
    ],
)
def test_agent_spec_rejects_unknown_channels_and_tools(
    field: str,
    value: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        build_spec(**{field: value})


def test_agent_draft_requires_matching_owner_and_aware_creation_time() -> None:
    spec = build_spec()

    with pytest.raises(ValidationError, match="owner_id"):
        AgentDraft(
            draft_id="draft-1",
            owner_id="family-2",
            source_message_id="om_message_1",
            spec=spec,
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="timezone-aware"):
        AgentDraft(
            draft_id="draft-1",
            owner_id=spec.owner_id,
            source_message_id="om_message_1",
            spec=spec,
            created_at=datetime(2026, 8, 29, 12, 0),
        )


def test_agent_execution_validates_its_timestamp_window() -> None:
    with pytest.raises(ValidationError, match="completed_at"):
        AgentExecution(
            execution_id="execution-1",
            agent_id="agent-weather-1",
            trigger_id="trigger-1",
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=NOW,
            completed_at=datetime(2026, 8, 29, 11, 59, tzinfo=UTC),
            output_text="今天有雨。",
            error=None,
        )
