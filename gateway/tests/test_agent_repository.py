from datetime import UTC, datetime, time, timedelta

import pytest

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
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def build_spec(**overrides: object) -> AgentSpec:
    data: dict[str, object] = {
        "agent_id": "agent-weather-1",
        "owner_id": "family-1",
        "name": "出门穿衣提醒",
        "kind": AgentKind.WEATHER,
        "enabled": True,
        "trigger": AgentTrigger(
            kind=TriggerKind.DAILY,
            timezone="Asia/Shanghai",
            local_time=time(7, 30),
        ),
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


def build_draft(**overrides: object) -> AgentDraft:
    spec = build_spec()
    data: dict[str, object] = {
        "draft_id": "draft-weather-1",
        "owner_id": spec.owner_id,
        "source_message_id": "om_message_weather_1",
        "spec": spec,
        "created_at": NOW,
    }
    data.update(overrides)
    return AgentDraft.model_validate(data)


def build_execution(agent_id: str) -> AgentExecution:
    return AgentExecution(
        execution_id="execution-weather-1",
        agent_id=agent_id,
        trigger_id="trigger-weather-1",
        status=AgentExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
        output_text="今天有雨，建议带伞。",
        error=None,
    )


def test_draft_is_owner_scoped_idempotent_by_source_message_and_survives_reopen(
    tmp_path,
) -> None:
    database_path = tmp_path / "agents.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()
    draft = build_draft()

    created = repository.create_draft(draft)
    duplicate = repository.create_draft(
        build_draft(draft_id="draft-weather-duplicate")
    )

    restarted = SQLiteTaskRepository(database_path)
    restarted.initialize()

    assert created == draft
    assert duplicate == draft
    assert restarted.get_draft_by_source(
        owner_id="family-1",
        source_message_id=draft.source_message_id,
    ) == draft
    assert restarted.get_draft_by_source(
        owner_id="family-2",
        source_message_id=draft.source_message_id,
    ) is None
    assert restarted.get_draft(draft.draft_id, owner_id="family-1") == draft
    assert restarted.get_draft(draft.draft_id, owner_id="family-2") is None


def test_confirm_draft_is_atomic_idempotent_and_owner_scoped(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "agents.db")
    repository.initialize()
    draft = build_draft()
    repository.create_draft(draft)

    confirmed, created = repository.confirm_draft(
        draft.draft_id,
        owner_id=draft.owner_id,
    )
    replayed, replay_created = repository.confirm_draft(
        draft.draft_id,
        owner_id=draft.owner_id,
    )

    assert (confirmed, created) == (draft.spec, True)
    assert (replayed, replay_created) == (draft.spec, False)
    assert repository.list_agents(owner_id=draft.owner_id) == [draft.spec]
    assert repository.get_agent(draft.spec.agent_id, owner_id=draft.owner_id) == draft.spec
    assert repository.get_agent(draft.spec.agent_id, owner_id="family-2") is None
    with pytest.raises(KeyError):
        repository.confirm_draft(draft.draft_id, owner_id="family-2")


def test_agent_update_delete_and_execution_history_enforce_owner_scope(tmp_path) -> None:
    database_path = tmp_path / "agents.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()
    draft = build_draft()
    repository.create_draft(draft)
    confirmed, _created = repository.confirm_draft(
        draft.draft_id,
        owner_id=draft.owner_id,
    )
    updated = confirmed.model_copy(
        update={"enabled": False, "name": "暂停的穿衣提醒"}
    )

    assert repository.update_agent(updated, owner_id="family-1") == updated
    with pytest.raises(ValueError, match="owner_id"):
        repository.update_agent(updated, owner_id="family-2")

    execution = build_execution(updated.agent_id)
    assert repository.record_execution(execution, owner_id="family-1") == execution
    with pytest.raises(PermissionError, match="owner"):
        repository.record_execution(
            execution.model_copy(update={"execution_id": "execution-cross-owner"}),
            owner_id="family-2",
        )

    restarted = SQLiteTaskRepository(database_path)
    restarted.initialize()
    assert restarted.list_executions(updated.agent_id, owner_id="family-1") == [
        execution
    ]
    assert restarted.list_executions(updated.agent_id, owner_id="family-2") == []
    assert restarted.delete_agent(updated.agent_id, owner_id="family-2") is False
    assert restarted.delete_agent(updated.agent_id, owner_id="family-1") is True
    assert restarted.get_agent(updated.agent_id, owner_id="family-1") is None


def test_execution_identity_cannot_be_redirected_on_idempotent_update(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "agents.db")
    repository.initialize()
    draft = build_draft()
    repository.create_draft(draft)
    repository.confirm_draft(draft.draft_id, owner_id=draft.owner_id)
    execution = build_execution(draft.spec.agent_id)
    repository.record_execution(execution, owner_id=draft.owner_id)

    with pytest.raises(ValueError, match="execution identity"):
        repository.record_execution(
            execution.model_copy(update={"trigger_id": "different-trigger"}),
            owner_id=draft.owner_id,
        )


def test_execution_claim_is_atomic_owner_scoped_and_idempotent(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "agents.db")
    repository.initialize()
    draft = build_draft()
    repository.create_draft(draft)
    repository.confirm_draft(draft.draft_id, owner_id=draft.owner_id)
    started = AgentExecution(
        execution_id="execution-claim",
        agent_id=draft.spec.agent_id,
        trigger_id="trigger-claim",
        status=AgentExecutionStatus.STARTED,
        started_at=NOW,
    )

    claimed, created = repository.claim_execution(started, owner_id=draft.owner_id)
    replayed, replay_created = repository.claim_execution(
        started,
        owner_id=draft.owner_id,
    )

    assert (claimed, created) == (started, True)
    assert (replayed, replay_created) == (started, False)
    with pytest.raises(PermissionError, match="owner"):
        repository.claim_execution(
            started.model_copy(update={"execution_id": "cross-owner-claim"}),
            owner_id="family-2",
        )


def test_deleted_confirmed_agent_cannot_be_resurrected_by_replay(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "agents.db")
    repository.initialize()
    draft = build_draft()
    repository.create_draft(draft)
    repository.confirm_draft(draft.draft_id, owner_id=draft.owner_id)
    assert repository.delete_agent(draft.spec.agent_id, owner_id=draft.owner_id)

    with pytest.raises(ValueError, match="deleted"):
        repository.confirm_draft(draft.draft_id, owner_id=draft.owner_id)

    assert repository.get_agent(draft.spec.agent_id, owner_id=draft.owner_id) is None


def test_deleted_agent_id_cannot_be_reused_by_another_owner(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "agents.db")
    repository.initialize()
    original = build_draft()
    repository.create_draft(original)
    repository.confirm_draft(original.draft_id, owner_id=original.owner_id)
    assert repository.delete_agent(original.spec.agent_id, owner_id=original.owner_id)

    other_spec = original.spec.model_copy(update={"owner_id": "family-2"})
    other_draft = build_draft(
        draft_id="draft-other-owner",
        owner_id="family-2",
        source_message_id="om_other_owner",
        spec=other_spec,
    )
    repository.create_draft(other_draft)

    with pytest.raises(ValueError, match="deleted"):
        repository.confirm_draft(other_draft.draft_id, owner_id="family-2")

    with pytest.raises(ValueError, match="deleted"):
        repository.confirm_draft(original.draft_id, owner_id=original.owner_id)
