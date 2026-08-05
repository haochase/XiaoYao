import pytest

from companion_gateway.domain.tasks import (
    InvalidTaskTransition,
    TaskEventType,
    TaskStatus,
    transition,
)


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    [
        (TaskStatus.CREATED, TaskEventType.SCHEDULED, TaskStatus.SCHEDULED),
        (TaskStatus.CREATED, TaskEventType.CANCELLED, TaskStatus.CANCELLED),
        (TaskStatus.SCHEDULED, TaskEventType.DUE, TaskStatus.DUE),
        (TaskStatus.SCHEDULED, TaskEventType.CANCELLED, TaskStatus.CANCELLED),
        (TaskStatus.DUE, TaskEventType.PENDING_DELIVERY, TaskStatus.PENDING_DELIVERY),
        (TaskStatus.DUE, TaskEventType.DELIVERING, TaskStatus.DELIVERING),
        (TaskStatus.DUE, TaskEventType.EXPIRED, TaskStatus.EXPIRED),
        (TaskStatus.DUE, TaskEventType.FAILED, TaskStatus.FAILED),
        (
            TaskStatus.PENDING_DELIVERY,
            TaskEventType.DELIVERING,
            TaskStatus.DELIVERING,
        ),
        (TaskStatus.PENDING_DELIVERY, TaskEventType.EXPIRED, TaskStatus.EXPIRED),
        (TaskStatus.DELIVERING, TaskEventType.DELIVERED, TaskStatus.DELIVERED),
        (TaskStatus.DELIVERING, TaskEventType.FAILED, TaskStatus.FAILED),
        (
            TaskStatus.DELIVERED,
            TaskEventType.ACKNOWLEDGED,
            TaskStatus.ACKNOWLEDGED,
        ),
        (TaskStatus.DELIVERED, TaskEventType.REJECTED, TaskStatus.REJECTED),
        (TaskStatus.DELIVERED, TaskEventType.EXPIRED, TaskStatus.EXPIRED),
    ],
)
def test_allowed_task_transition(
    current: TaskStatus,
    event: TaskEventType,
    expected: TaskStatus,
) -> None:
    assert transition(current, event) is expected


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (TaskStatus.CREATED, TaskEventType.ACKNOWLEDGED),
        (TaskStatus.DUE, TaskEventType.DELIVERED),
        (TaskStatus.DELIVERING, TaskEventType.DELIVERING),
        (TaskStatus.DELIVERED, TaskEventType.DELIVERED),
        (TaskStatus.ACKNOWLEDGED, TaskEventType.DELIVERING),
        (TaskStatus.REJECTED, TaskEventType.ACKNOWLEDGED),
        (TaskStatus.EXPIRED, TaskEventType.DELIVERING),
        (TaskStatus.FAILED, TaskEventType.DELIVERING),
        (TaskStatus.CANCELLED, TaskEventType.SCHEDULED),
    ],
)
def test_invalid_task_transition_is_rejected(
    current: TaskStatus,
    event: TaskEventType,
) -> None:
    with pytest.raises(
        InvalidTaskTransition,
        match=f"cannot apply {event.value} to {current.value}",
    ):
        transition(current, event)
