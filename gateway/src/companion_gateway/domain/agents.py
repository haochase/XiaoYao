from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.domain.models import ContentText, Identifier


class AgentKind(StrEnum):
    REMINDER = "reminder"
    MEDICATION = "medication"
    COMPANION = "companion"
    ENGLISH = "english_practice"
    WEATHER = "weather_clothing"
    DAILY_SUMMARY = "daily_summary"
    IMAGE = "image_observation"


class TriggerKind(StrEnum):
    MANUAL = "manual"
    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"


class AgentChannel(StrEnum):
    FEISHU = "feishu"
    ESP32 = "esp32"


class AgentToolName(StrEnum):
    CREATE_REMINDER = "create_reminder"
    QUERY_TASK_STATUS = "query_task_status"
    WEATHER_FORECAST = "weather_forecast"
    READ_CONFIRMED_MEMORY = "read_confirmed_memory"
    PROPOSE_MEMORY = "propose_memory"
    SEND_FEISHU = "send_feishu"
    SPEAK_ESP32 = "speak_esp32"
    DAILY_SUMMARY = "daily_summary"


class AgentMemoryPolicy(StrEnum):
    NONE = "none"
    READ_CONFIRMED = "read_confirmed"
    PROPOSE_CONFIRMED = "propose_confirmed"


class AgentExecutionStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class AgentTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TriggerKind
    timezone: Identifier | None = None
    at: datetime | None = None
    local_time: time | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value)

    @field_validator("local_time")
    @classmethod
    def validate_local_time(cls, value: time | None) -> time | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("local_time must not include a timezone")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> "AgentTrigger":
        if self.kind is TriggerKind.MANUAL:
            if (
                self.at is not None
                or self.local_time is not None
                or self.timezone is not None
            ):
                raise ValueError(
                    "manual trigger must not include at, local_time, or timezone"
                )
            return self
        if self.kind is TriggerKind.ONCE:
            if self.at is None or self.timezone is None:
                raise ValueError("once trigger requires aware at and timezone")
            if self.local_time is not None:
                raise ValueError("once trigger must not include local_time")
            return self
        if self.at is not None:
            raise ValueError("daily and weekdays triggers must not include at")
        if self.local_time is None or self.timezone is None:
            raise ValueError("daily and weekdays triggers require local_time and timezone")
        return self


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: Identifier
    owner_id: Identifier
    name: ContentText
    kind: AgentKind
    enabled: bool
    trigger: AgentTrigger
    channels: tuple[AgentChannel, ...] = Field(min_length=1)
    allowed_tools: tuple[AgentToolName, ...]
    prompt: ContentText
    memory_policy: AgentMemoryPolicy
    max_turns: int = Field(ge=1)
    config: dict[str, object]


class AgentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_id: Identifier
    owner_id: Identifier
    source_message_id: Identifier
    spec: AgentSpec
    created_at: datetime

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)

    @model_validator(mode="after")
    def validate_spec_owner(self) -> "AgentDraft":
        if self.spec.owner_id != self.owner_id:
            raise ValueError("draft owner_id must match spec.owner_id")
        return self


class AgentExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: Identifier
    agent_id: Identifier
    trigger_id: Identifier
    status: AgentExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    output_text: ContentText | None = None
    error: ContentText | None = None

    _validate_started_at = field_validator("started_at")(_require_timezone_aware)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value)

    @model_validator(mode="after")
    def validate_completion_window(self) -> "AgentExecution":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class AgentRepository(Protocol):
    def create_draft(self, draft: AgentDraft) -> AgentDraft: ...

    def get_draft(self, draft_id: str, *, owner_id: str) -> AgentDraft | None: ...

    def get_draft_by_source(
        self,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft | None: ...

    def confirm_draft(
        self,
        draft_id: str,
        *,
        owner_id: str,
    ) -> tuple[AgentSpec, bool]: ...

    def list_agents(self, *, owner_id: str) -> list[AgentSpec]: ...

    def get_agent(self, agent_id: str, *, owner_id: str) -> AgentSpec | None: ...

    def update_agent(self, agent: AgentSpec, *, owner_id: str) -> AgentSpec: ...

    def delete_agent(self, agent_id: str, *, owner_id: str) -> bool: ...

    def record_execution(
        self,
        execution: AgentExecution,
        *,
        owner_id: str,
    ) -> AgentExecution: ...

    def claim_execution(
        self,
        execution: AgentExecution,
        *,
        owner_id: str,
    ) -> tuple[AgentExecution, bool]: ...

    def list_executions(
        self,
        agent_id: str,
        *,
        owner_id: str,
    ) -> list[AgentExecution]: ...
