from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from companion_gateway.agent.router import AgentCommandRouter
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


NOW = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)


def build_agent(
    *,
    agent_id: str,
    name: str,
    kind: AgentKind,
    config: dict[str, object] | None = None,
    prompt: str = "initial prompt",
    owner_id: str = "owner-1",
) -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        owner_id=owner_id,
        name=name,
        kind=kind,
        enabled=True,
        trigger=AgentTrigger(kind=TriggerKind.MANUAL),
        channels=(AgentChannel.FEISHU,),
        allowed_tools=(AgentToolName.SEND_FEISHU,),
        prompt=prompt,
        memory_policy=AgentMemoryPolicy.NONE,
        max_turns=3,
        config=config or {},
    )


def build_draft(agent: AgentSpec) -> AgentDraft:
    return AgentDraft(
        draft_id="draft-1",
        owner_id=agent.owner_id,
        source_message_id="message-create",
        spec=agent,
        created_at=NOW,
    )


@dataclass
class FakeRegistry:
    proposed_draft: AgentDraft
    agents: list[AgentSpec] = field(default_factory=list)
    propose_calls: list[tuple[str, str, str]] = field(default_factory=list)
    confirm_calls: list[tuple[str, str]] = field(default_factory=list)
    lifecycle_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def propose(self, request_text: str, *, owner_id: str, source_message_id: str) -> AgentDraft:
        self.propose_calls.append((request_text, owner_id, source_message_id))
        return self.proposed_draft

    def confirm(self, draft_id: str, *, owner_id: str) -> AgentSpec:
        self.confirm_calls.append((draft_id, owner_id))
        if self.proposed_draft.owner_id != owner_id:
            raise KeyError(draft_id)
        if self.proposed_draft.spec not in self.agents:
            self.agents.append(self.proposed_draft.spec)
        return self.proposed_draft.spec

    def list(self, *, owner_id: str) -> list[AgentSpec]:
        return [agent for agent in self.agents if agent.owner_id == owner_id]

    def get(self, agent_id: str, *, owner_id: str) -> AgentSpec | None:
        for agent in self.list(owner_id=owner_id):
            if agent.agent_id == agent_id:
                return agent
        return None

    def pause(self, agent_id: str, *, owner_id: str) -> AgentSpec:
        return self._set_enabled("pause", agent_id, owner_id, False)

    def resume(self, agent_id: str, *, owner_id: str) -> AgentSpec:
        return self._set_enabled("resume", agent_id, owner_id, True)

    def delete(self, agent_id: str, *, owner_id: str) -> bool:
        self.lifecycle_calls.append(("delete", agent_id, owner_id))
        agent = self.get(agent_id, owner_id=owner_id)
        if agent is None:
            return False
        self.agents.remove(agent)
        return True

    def _set_enabled(
        self,
        action: str,
        agent_id: str,
        owner_id: str,
        enabled: bool,
    ) -> AgentSpec:
        self.lifecycle_calls.append((action, agent_id, owner_id))
        agent = self.get(agent_id, owner_id=owner_id)
        if agent is None:
            raise KeyError(agent_id)
        updated = agent.model_copy(update={"enabled": enabled})
        self.agents[self.agents.index(agent)] = updated
        return updated


@dataclass
class FakeRuntime:
    calls: list[tuple[str, str, str, datetime]] = field(default_factory=list)

    def run(
        self,
        agent_id: str,
        *,
        owner_id: str,
        trigger_id: str,
        now: datetime,
    ) -> AgentExecution:
        self.calls.append((agent_id, owner_id, trigger_id, now))
        return AgentExecution(
            execution_id="execution-1",
            agent_id=agent_id,
            trigger_id=trigger_id,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            output_text="manual result",
        )


def router_with_agents(*agents: AgentSpec) -> tuple[AgentCommandRouter, FakeRegistry, FakeRuntime]:
    proposed = build_draft(
        build_agent(
            agent_id="agent-proposed",
            name="created agent",
            kind=AgentKind.REMINDER,
            config={"message": "drink water"},
        )
    )
    registry = FakeRegistry(proposed_draft=proposed, agents=list(agents))
    runtime = FakeRuntime()
    return (
        AgentCommandRouter(registry=registry, runtime=runtime, clock=lambda: NOW),
        registry,
        runtime,
    )


def handle(router: AgentCommandRouter, text: str, *, source: str = "message-1"):
    return router.handle(
        text=text,
        owner_id="owner-1",
        chat_id="chat-1",
        source_message_id=source,
    )


def test_router_creates_confirms_and_cancels_chat_scoped_drafts() -> None:
    router, registry, _runtime = router_with_agents()

    proposed = handle(router, "\u521b\u5efa\u4e00\u4e2a\u559d\u6c34\u63d0\u9192", source="message-create")
    confirmed = handle(router, "\u786e\u8ba4\u521b\u5efa", source="message-confirm")
    missing = handle(router, "\u786e\u8ba4\u521b\u5efa", source="message-confirm-again")
    handle(router, "\u521b\u5efa\u4e00\u4e2a\u65b0\u63d0\u9192", source="message-create-2")
    cancelled = handle(router, "\u53d6\u6d88\u521b\u5efa", source="message-cancel")

    assert proposed.handled is True
    assert "草稿" in proposed.reply
    assert registry.propose_calls == [
        ("\u559d\u6c34\u63d0\u9192", "owner-1", "message-create"),
        ("\u65b0\u63d0\u9192", "owner-1", "message-create-2"),
    ]
    assert confirmed.handled is True
    assert "已创建智能体" in confirmed.reply
    assert registry.confirm_calls == [("draft-1", "owner-1")]
    assert missing.handled is True
    assert "没有待确认" in missing.reply
    assert cancelled.handled is True
    assert "已取消创建" in cancelled.reply


def test_router_routes_natural_language_timed_reminders_to_confirmation_flow() -> None:
    router, registry, _runtime = router_with_agents()

    proposed = handle(router, "今天零点十二分提醒我去洗澡", source="message-timed-reminder")

    assert proposed.handled is True
    assert "草稿" in proposed.reply
    assert registry.propose_calls == [
        ("今天零点十二分提醒我去洗澡", "owner-1", "message-timed-reminder"),
    ]


def test_router_lists_manages_unique_names_and_rejects_ambiguous_or_other_owner_agents() -> None:
    reminder = build_agent(
        agent_id="agent-reminder",
        name="water",
        kind=AgentKind.REMINDER,
        config={"message": "drink water"},
    )
    duplicate = build_agent(
        agent_id="agent-duplicate",
        name="water",
        kind=AgentKind.REMINDER,
        config={"message": "drink water"},
    )
    foreign = build_agent(
        agent_id="agent-foreign",
        name="private",
        kind=AgentKind.REMINDER,
        owner_id="owner-2",
    )
    router, registry, runtime = router_with_agents(reminder, duplicate, foreign)

    listed = handle(router, "\u6211\u7684\u667a\u80fd\u4f53")
    ambiguous = handle(router, "\u8fd0\u884c water", source="message-run-ambiguous")
    registry.agents.remove(duplicate)
    run = handle(router, "\u8fd0\u884c water", source="message-run")
    pause = handle(router, "\u6682\u505c water")
    resume = handle(router, "\u6062\u590d water")
    deleted = handle(router, "\u5220\u9664 water")
    missing = handle(router, "\u8fd0\u884c private")

    assert listed.handled is True
    assert "water" in listed.reply
    assert ambiguous.handled is True
    assert "同名智能体" in ambiguous.reply
    assert run.reply == "manual result"
    assert runtime.calls == [("agent-reminder", "owner-1", runtime.calls[0][2], NOW)]
    assert pause.handled is True
    assert resume.handled is True
    assert deleted.handled is True
    assert registry.lifecycle_calls == [
        ("pause", "agent-reminder", "owner-1"),
        ("resume", "agent-reminder", "owner-1"),
        ("delete", "agent-reminder", "owner-1"),
    ]
    assert missing.handled is True
    assert "没有找到" in missing.reply


def test_router_activates_companion_and_english_sessions_per_owner_and_chat() -> None:
    companion = build_agent(
        agent_id="agent-companion",
        name="companion",
        kind=AgentKind.COMPANION,
        prompt="companion initial prompt",
    )
    english = build_agent(
        agent_id="agent-english",
        name="english",
        kind=AgentKind.ENGLISH,
        config={"level": "intermediate", "scenario": "interview"},
        prompt="english initial prompt",
    )
    router, _registry, _runtime = router_with_agents(companion, english)

    entered = handle(router, "\u8fdb\u5165\u966a\u4f34\u6a21\u5f0f")
    continued = handle(router, "\u4eca\u5929\u6709\u70b9\u7d2f", source="message-companion")
    exited = handle(router, "\u9000\u51fa\u5f53\u524d\u6a21\u5f0f")
    ordinary = handle(router, "\u4f60\u597d", source="message-ordinary")
    english_started = handle(router, "\u5f00\u59cb\u4e2d\u7ea7\u9762\u8bd5\u82f1\u8bed\u7ec3\u4e60")

    assert entered.handled is True
    assert entered.reply != "companion initial prompt"
    assert continued.handled is False
    assert continued.reply is None
    assert "companion initial prompt" not in router.active_context(
        owner_id="owner-1",
        chat_id="chat-1",
    )
    assert exited.handled is True
    assert ordinary.handled is False
    assert ordinary.reply is None
    assert english_started.handled is True
    assert english_started.reply != "english initial prompt"
    assert "english initial prompt" not in router.active_context(
        owner_id="owner-1",
        chat_id="chat-1",
    )


def test_router_exposes_only_one_active_context_for_voice_owner() -> None:
    companion = build_agent(
        agent_id="agent-companion",
        name="companion",
        kind=AgentKind.COMPANION,
        prompt="companion initial prompt",
    )
    router, _registry, _runtime = router_with_agents(companion)

    handle(router, "\u8fdb\u5165\u966a\u4f34\u6a21\u5f0f")

    assert router.active_context_for_owner(owner_id="owner-1") == router.active_context(
        owner_id="owner-1",
        chat_id="chat-1",
    )
    assert router.active_context_for_owner(owner_id="owner-2") == ""
