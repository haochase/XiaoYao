from dataclasses import dataclass, field
from datetime import UTC, datetime
import json

import pytest

from companion_gateway.agent.compiler import (
    AgentSpecCompileError,
    MimoAgentSpecCompiler,
)
from companion_gateway.domain.agents import AgentKind, AgentToolName, TriggerKind


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


@dataclass
class FakeMimoRuntime:
    reply: str
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def respond(self, text: str, *, history: tuple[object, ...] = ()) -> str:
        self.calls.append((text, history))
        return self.reply


def candidate_for(
    kind: AgentKind,
    *,
    trigger: dict[str, object],
    allowed_tools: list[str],
) -> dict[str, object]:
    return {
        "agent_id": "model-agent-id",
        "owner_id": "model-owner-id",
        "draft_id": "model-draft-id",
        "source_message_id": "model-source-id",
        "name": f"{kind.value} agent",
        "kind": kind.value,
        "enabled": True,
        "trigger": trigger,
        "channels": ["feishu"],
        "allowed_tools": allowed_tools,
        "prompt": "Use the configured gateway tools only.",
        "memory_policy": "none",
        "max_turns": 3,
        "config": {},
    }


@pytest.mark.parametrize(
    ("kind", "trigger", "allowed_tools"),
    [
        (
            AgentKind.REMINDER,
            {"kind": TriggerKind.MANUAL.value},
            [AgentToolName.CREATE_REMINDER.value],
        ),
        (
            AgentKind.WEATHER,
            {
                "kind": TriggerKind.DAILY.value,
                "timezone": "Asia/Shanghai",
                "local_time": "07:30",
            },
            [AgentToolName.WEATHER_FORECAST.value],
        ),
        (
            AgentKind.COMPANION,
            {"kind": TriggerKind.MANUAL.value},
            [],
        ),
        (
            AgentKind.ENGLISH,
            {"kind": TriggerKind.MANUAL.value},
            [AgentToolName.SPEAK_ESP32.value],
        ),
    ],
)
def test_compiler_builds_four_agent_kinds_and_overrides_model_identity(
    kind: AgentKind,
    trigger: dict[str, object],
    allowed_tools: list[str],
) -> None:
    runtime = FakeMimoRuntime(
        json.dumps(
            candidate_for(
                kind,
                trigger=trigger,
                allowed_tools=allowed_tools,
            )
        )
    )
    identifiers = iter(("gateway-agent-id", "gateway-draft-id"))
    compiler = MimoAgentSpecCompiler(
        runtime=runtime,
        clock=lambda: NOW,
        id_factory=lambda: next(identifiers),
    )

    draft = compiler.compile(
        "请创建这个智能体",
        owner_id="owner-1",
        source_message_id="message-1",
    )

    assert draft.draft_id == "gateway-draft-id"
    assert draft.source_message_id == "message-1"
    assert draft.owner_id == "owner-1"
    assert draft.spec.agent_id == "gateway-agent-id"
    assert draft.spec.owner_id == "owner-1"
    assert draft.spec.kind is kind
    assert draft.spec.allowed_tools == tuple(AgentToolName(item) for item in allowed_tools)
    assert draft.created_at == NOW
    assert runtime.calls[0][1] == ()
    assert "请创建这个智能体" in runtime.calls[0][0]
    assert "JSON" in runtime.calls[0][0]


def test_compiler_rejects_mimo_non_json_output() -> None:
    compiler = MimoAgentSpecCompiler(
        runtime=FakeMimoRuntime("not json"),
        clock=lambda: NOW,
        id_factory=lambda: "generated-id",
    )

    with pytest.raises(AgentSpecCompileError, match="valid JSON"):
        compiler.compile(
            "帮我创建提醒",
            owner_id="owner-1",
            source_message_id="message-1",
        )


def test_compiler_rejects_unknown_gateway_tools_before_creating_a_draft() -> None:
    runtime = FakeMimoRuntime(
        json.dumps(
            candidate_for(
                AgentKind.REMINDER,
                trigger={"kind": "manual"},
                allowed_tools=["exec_python"],
            )
        )
    )
    compiler = MimoAgentSpecCompiler(
        runtime=runtime,
        clock=lambda: NOW,
        id_factory=lambda: "generated-id",
    )

    with pytest.raises(AgentSpecCompileError, match="allowed tool"):
        compiler.compile(
            "帮我创建提醒",
            owner_id="owner-1",
            source_message_id="message-1",
        )
