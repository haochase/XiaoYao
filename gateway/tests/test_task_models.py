from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskEvent,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.domain.tasks import TaskEventType


def valid_task_data() -> dict[str, object]:
    return {
        "actor_id": "family-1",
        "target_device_id": "living-room",
        "kind": "reminder",
        "schedule": {
            "at": "2026-08-05T20:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "payload": {"text": "take medicine"},
        "confirmation_policy": "required",
        "idempotency_key": "client:message-1",
    }


def test_task_create_accepts_the_design_contract() -> None:
    task = TaskCreate.model_validate(valid_task_data())

    assert task.kind is TaskKind.REMINDER
    assert task.confirmation_policy is ConfirmationPolicy.REQUIRED
    assert task.schedule.timezone == "Asia/Shanghai"
    assert task.schedule.at.utcoffset() is not None
    assert task.payload.text == "take medicine"


def test_task_create_rejects_naive_schedule() -> None:
    data = valid_task_data()
    data["schedule"] = {
        "at": datetime(2026, 8, 5, 20, 0),
        "timezone": "Asia/Shanghai",
    }

    with pytest.raises(ValidationError, match="timezone-aware"):
        TaskCreate.model_validate(data)


def test_task_create_rejects_unknown_timezone() -> None:
    data = valid_task_data()
    data["schedule"] = {
        "at": "2026-08-05T20:00:00+08:00",
        "timezone": "Mars/Olympus",
    }

    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        TaskCreate.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", "   "),
        ("target_device_id", ""),
        ("idempotency_key", "\t"),
    ],
)
def test_task_create_rejects_blank_identity_fields(field: str, value: str) -> None:
    data = valid_task_data()
    data[field] = value

    with pytest.raises(ValidationError):
        TaskCreate.model_validate(data)


def test_task_payload_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        TaskPayload(text="   ")


def test_task_contract_serializes_exact_enum_values() -> None:
    task = TaskCreate.model_validate(valid_task_data())

    assert task.model_dump(mode="json") == {
        "actor_id": "family-1",
        "target_device_id": "living-room",
        "kind": "reminder",
        "schedule": {
            "at": "2026-08-05T20:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "payload": {"text": "take medicine"},
        "confirmation_policy": "required",
        "idempotency_key": "client:message-1",
    }


def test_created_event_is_a_valid_append_only_event() -> None:
    event = TaskEvent(
        event_id="evt-1",
        task_id="tsk-1",
        type="created",
        reason="task_created",
        occurred_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        trace_id="trc-1",
    )

    assert event.type is TaskEventType.CREATED


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TaskEvent(
            event_id="evt-1",
            task_id="tsk-1",
            type="created",
            reason="task_created",
            occurred_at=datetime(2026, 8, 5, 12, 0),
            trace_id="trc-1",
        )


def test_schedule_can_be_built_directly() -> None:
    schedule = TaskSchedule(
        at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )

    assert schedule.at.tzinfo is UTC
