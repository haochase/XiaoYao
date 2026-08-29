from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from companion_gateway.agent.registry import AgentRegistry
from companion_gateway.domain.agents import (
    AgentChannel,
    AgentDraft,
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
        "agent_id": "agent-1",
        "owner_id": "owner-1",
        "name": "Drink water",
        "kind": AgentKind.REMINDER,
        "enabled": True,
        "trigger": AgentTrigger(kind=TriggerKind.MANUAL),
        "channels": (AgentChannel.FEISHU,),
        "allowed_tools": (AgentToolName.CREATE_REMINDER,),
        "prompt": "Create confirmed reminders only.",
        "memory_policy": AgentMemoryPolicy.NONE,
        "max_turns": 3,
        "config": {},
    }
    data.update(overrides)
    return AgentSpec.model_validate(data)


def build_draft(
    *,
    draft_id: str = "draft-1",
    source_message_id: str = "message-1",
    spec: AgentSpec | None = None,
) -> AgentDraft:
    value = spec or build_spec()
    return AgentDraft(
        draft_id=draft_id,
        owner_id=value.owner_id,
        source_message_id=source_message_id,
        spec=value,
        created_at=NOW,
    )


@dataclass
class FakeCompiler:
    drafts: list[AgentDraft]
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def compile(
        self,
        request_text: str,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft:
        self.calls.append((request_text, owner_id, source_message_id))
        return self.drafts.pop(0)


@dataclass
class FakeRepository:
    drafts: dict[str, AgentDraft] = field(default_factory=dict)
    drafts_by_source: dict[tuple[str, str], str] = field(default_factory=dict)
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    confirmed: set[str] = field(default_factory=set)

    def create_draft(self, draft: AgentDraft) -> AgentDraft:
        source_key = (draft.owner_id, draft.source_message_id)
        existing_id = self.drafts_by_source.get(source_key)
        if existing_id is not None:
            return self.drafts[existing_id]
        self.drafts[draft.draft_id] = draft
        self.drafts_by_source[source_key] = draft.draft_id
        return draft

    def get_draft(self, draft_id: str, *, owner_id: str) -> AgentDraft | None:
        draft = self.drafts.get(draft_id)
        return draft if draft is not None and draft.owner_id == owner_id else None

    def get_draft_by_source(
        self,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft | None:
        draft_id = self.drafts_by_source.get((owner_id, source_message_id))
        return self.drafts.get(draft_id) if draft_id is not None else None

    def confirm_draft(
        self,
        draft_id: str,
        *,
        owner_id: str,
    ) -> tuple[AgentSpec, bool]:
        draft = self.get_draft(draft_id, owner_id=owner_id)
        if draft is None:
            raise KeyError(draft_id)
        created = draft_id not in self.confirmed
        self.confirmed.add(draft_id)
        self.agents[draft.spec.agent_id] = draft.spec
        return draft.spec, created

    def list_agents(self, *, owner_id: str) -> list[AgentSpec]:
        return [agent for agent in self.agents.values() if agent.owner_id == owner_id]

    def get_agent(self, agent_id: str, *, owner_id: str) -> AgentSpec | None:
        agent = self.agents.get(agent_id)
        return agent if agent is not None and agent.owner_id == owner_id else None

    def update_agent(self, agent: AgentSpec, *, owner_id: str) -> AgentSpec:
        if agent.owner_id != owner_id:
            raise ValueError("owner_id mismatch")
        if self.get_agent(agent.agent_id, owner_id=owner_id) is None:
            raise KeyError(agent.agent_id)
        self.agents[agent.agent_id] = agent
        return agent

    def delete_agent(self, agent_id: str, *, owner_id: str) -> bool:
        if self.get_agent(agent_id, owner_id=owner_id) is None:
            return False
        del self.agents[agent_id]
        return True


def test_propose_delegates_duplicate_messages_to_the_owner_scoped_repository() -> None:
    first = build_draft()
    duplicate = build_draft(draft_id="draft-2")
    compiler = FakeCompiler([first, duplicate])
    registry = AgentRegistry(repository=FakeRepository(), compiler=compiler)

    proposed = registry.propose(
        "提醒我喝水",
        owner_id="owner-1",
        source_message_id="message-1",
    )
    replayed = registry.propose(
        "提醒我喝水",
        owner_id="owner-1",
        source_message_id="message-1",
    )

    assert proposed == first
    assert replayed == first
    assert compiler.calls == [("提醒我喝水", "owner-1", "message-1")]


def test_confirm_pause_resume_list_get_and_delete_are_owner_scoped() -> None:
    draft = build_draft()
    repository = FakeRepository()
    repository.create_draft(draft)
    registry = AgentRegistry(repository=repository, compiler=FakeCompiler([]))

    confirmed = registry.confirm(draft.draft_id, owner_id="owner-1")
    paused = registry.pause(confirmed.agent_id, owner_id="owner-1")
    resumed = registry.resume(confirmed.agent_id, owner_id="owner-1")

    assert confirmed == draft.spec
    assert paused.enabled is False
    assert resumed.enabled is True
    assert registry.list(owner_id="owner-1") == [resumed]
    assert registry.get(resumed.agent_id, owner_id="owner-1") == resumed
    assert registry.get(resumed.agent_id, owner_id="owner-2") is None
    with pytest.raises(KeyError):
        registry.pause(resumed.agent_id, owner_id="owner-2")
    assert registry.delete(resumed.agent_id, owner_id="owner-2") is False
    assert registry.delete(resumed.agent_id, owner_id="owner-1") is True
    assert registry.get(resumed.agent_id, owner_id="owner-1") is None


def test_confirm_rejects_another_owners_draft() -> None:
    draft = build_draft()
    repository = FakeRepository()
    repository.create_draft(draft)
    registry = AgentRegistry(repository=repository, compiler=FakeCompiler([]))

    with pytest.raises(KeyError):
        registry.confirm(draft.draft_id, owner_id="owner-2")
