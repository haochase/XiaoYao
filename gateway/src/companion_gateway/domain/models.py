from datetime import datetime
from enum import StrEnum
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from companion_gateway.domain.tasks import TaskEventType, TaskStatus


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
ContentText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]


class TaskKind(StrEnum):
    REMINDER = "reminder"
    ANNOUNCEMENT = "announcement"
    ROUTINE = "routine"


class ConfirmationPolicy(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NONE = "none"


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class TaskSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    timezone: Identifier

    _validate_at = field_validator("at")(_require_timezone_aware)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class TaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: ContentText


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: Identifier
    target_device_id: Identifier
    kind: TaskKind
    schedule: TaskSchedule
    payload: TaskPayload
    confirmation_policy: ConfirmationPolicy
    idempotency_key: Identifier


class TaskRecord(TaskCreate):
    task_id: Identifier
    status: TaskStatus
    created_at: datetime
    trace_id: Identifier

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)


class TaskEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Identifier
    task_id: Identifier
    type: TaskEventType
    reason: ContentText | None = None
    occurred_at: datetime
    trace_id: Identifier

    _validate_occurred_at = field_validator("occurred_at")(_require_timezone_aware)
