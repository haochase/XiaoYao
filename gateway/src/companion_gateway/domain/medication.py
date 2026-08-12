from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from companion_gateway.domain.models import ContentText, Identifier


MEDICATION_TIMEZONE = "Asia/Shanghai"


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _require_optional_timezone_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_timezone_aware(value)


class MedicationOccurrenceStatus(StrEnum):
    SCHEDULED = "scheduled"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    ESCALATED = "escalated"


class FeishuFallbackStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class FeishuSendResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    message_id: Identifier | None = None
    error: ContentText | None = None


class MedicationPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: Identifier
    target_device_id: Identifier
    reminder_times: tuple[time, ...] = (time(8), time(12), time(20))
    timezone: Identifier = MEDICATION_TIMEZONE
    message: ContentText = "该吃药了，请确认已服药。"
    enabled: bool = True

    @field_validator("reminder_times")
    @classmethod
    def validate_reminder_times(cls, value: tuple[time, ...]) -> tuple[time, ...]:
        if not 1 <= len(value) <= 3:
            raise ValueError("reminder_times must contain one to three times")
        normalized = tuple(item.replace(second=0, microsecond=0) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("reminder_times must contain unique times")
        return tuple(sorted(normalized))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        if value != MEDICATION_TIMEZONE:
            raise ValueError(f"timezone must be {MEDICATION_TIMEZONE}")
        return value


class MedicationPlan(MedicationPlanCreate):
    plan_id: Identifier
    created_at: datetime
    updated_at: datetime

    _validate_created_at = field_validator("created_at", "updated_at")(
        _require_timezone_aware
    )


class MedicationOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    occurrence_id: Identifier
    plan_id: Identifier
    actor_id: Identifier
    target_device_id: Identifier
    local_date: date
    local_time: time
    scheduled_at: datetime
    ack_deadline_at: datetime
    task_id: Identifier | None = None
    status: MedicationOccurrenceStatus = MedicationOccurrenceStatus.SCHEDULED
    acknowledged_at: datetime | None = None
    feishu_status: FeishuFallbackStatus = FeishuFallbackStatus.PENDING
    feishu_message_id: Identifier | None = None
    feishu_error: ContentText | None = None
    created_at: datetime
    trace_id: Identifier

    _validate_timestamps = field_validator(
        "scheduled_at", "ack_deadline_at", "created_at"
    )(_require_timezone_aware)
    _validate_acknowledged_at = field_validator("acknowledged_at")(
        _require_optional_timezone_aware
    )

    @model_validator(mode="after")
    def validate_deadline(self) -> "MedicationOccurrence":
        if self.ack_deadline_at < self.scheduled_at:
            raise ValueError("ack_deadline_at must not precede scheduled_at")
        return self


class MedicationTickResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    created_occurrence_ids: tuple[str, ...] = ()
    scheduled_task_ids: tuple[str, ...] = ()
    delivered_occurrence_ids: tuple[str, ...] = ()
    fallback_occurrence_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class MedicationRepository(Protocol):
    def create_medication_plan(
        self,
        plan: MedicationPlanCreate,
        *,
        plan_id: str,
        occurred_at: datetime,
    ) -> tuple[MedicationPlan, bool]: ...

    def get_medication_plan(self, plan_id: str) -> MedicationPlan | None: ...

    def list_medication_plans(
        self, *, enabled: bool | None = None
    ) -> list[MedicationPlan]: ...

    def disable_medication_plan(
        self, plan_id: str, *, occurred_at: datetime
    ) -> MedicationPlan: ...

    def create_occurrence_if_absent(
        self, occurrence: MedicationOccurrence
    ) -> tuple[MedicationOccurrence, bool]: ...

    def get_medication_occurrence(
        self, occurrence_id: str
    ) -> MedicationOccurrence | None: ...

    def get_medication_occurrence_by_task_id(
        self,
        task_id: str,
    ) -> MedicationOccurrence | None: ...

    def list_medication_occurrences(
        self, *, statuses: tuple[MedicationOccurrenceStatus, ...] | None = None
    ) -> list[MedicationOccurrence]: ...

    def bind_occurrence_task(
        self, occurrence_id: str, *, task_id: str
    ) -> MedicationOccurrence: ...

    def mark_occurrence_delivered(
        self, occurrence_id: str
    ) -> MedicationOccurrence: ...

    def mark_occurrence_acknowledged(
        self, occurrence_id: str, *, occurred_at: datetime
    ) -> MedicationOccurrence: ...

    def claim_feishu_fallback(self, occurrence_id: str) -> bool: ...

    def complete_feishu_fallback(
        self,
        occurrence_id: str,
        *,
        status: FeishuFallbackStatus,
        message_id: str | None,
        error: str | None,
    ) -> MedicationOccurrence: ...
