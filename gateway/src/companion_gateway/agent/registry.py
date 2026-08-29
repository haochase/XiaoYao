from __future__ import annotations

from companion_gateway.agent.compiler import AgentSpecCompiler
from companion_gateway.domain.agents import AgentDraft, AgentRepository, AgentSpec


class AgentRegistry:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        compiler: AgentSpecCompiler,
    ) -> None:
        self._repository = repository
        self._compiler = compiler

    def propose(
        self,
        request_text: str,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft:
        draft = self._compiler.compile(
            request_text,
            owner_id=owner_id,
            source_message_id=source_message_id,
        )
        return self._repository.create_draft(draft)

    def confirm(self, draft_id: str, *, owner_id: str) -> AgentSpec:
        agent, _created = self._repository.confirm_draft(
            draft_id,
            owner_id=owner_id,
        )
        return agent

    def pause(self, agent_id: str, *, owner_id: str) -> AgentSpec:
        return self._set_enabled(agent_id, owner_id=owner_id, enabled=False)

    def resume(self, agent_id: str, *, owner_id: str) -> AgentSpec:
        return self._set_enabled(agent_id, owner_id=owner_id, enabled=True)

    def delete(self, agent_id: str, *, owner_id: str) -> bool:
        return self._repository.delete_agent(agent_id, owner_id=owner_id)

    def list(self, *, owner_id: str) -> list[AgentSpec]:
        return self._repository.list_agents(owner_id=owner_id)

    def get(self, agent_id: str, *, owner_id: str) -> AgentSpec | None:
        return self._repository.get_agent(agent_id, owner_id=owner_id)

    def _set_enabled(
        self,
        agent_id: str,
        *,
        owner_id: str,
        enabled: bool,
    ) -> AgentSpec:
        agent = self._repository.get_agent(agent_id, owner_id=owner_id)
        if agent is None:
            raise KeyError(agent_id)
        updated = agent.model_copy(update={"enabled": enabled})
        return self._repository.update_agent(updated, owner_id=owner_id)
