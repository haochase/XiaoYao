from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "created"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SCHEDULED = "scheduled"
    DUE = "due"
    PENDING_DELIVERY = "pending_delivery"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEventType(StrEnum):
    CREATED = "created"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    SCHEDULED = "scheduled"
    DUE = "due"
    PENDING_DELIVERY = "pending_delivery"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidTaskTransition(ValueError):
    """Raised when an event cannot be applied to the current task state."""


_TRANSITIONS = {
    (TaskStatus.CREATED, TaskEventType.SCHEDULED): TaskStatus.SCHEDULED,
    (
        TaskStatus.CREATED,
        TaskEventType.AWAITING_CONFIRMATION,
    ): TaskStatus.AWAITING_CONFIRMATION,
    (TaskStatus.CREATED, TaskEventType.CANCELLED): TaskStatus.CANCELLED,
    (
        TaskStatus.AWAITING_CONFIRMATION,
        TaskEventType.CONFIRMED,
    ): TaskStatus.SCHEDULED,
    (
        TaskStatus.AWAITING_CONFIRMATION,
        TaskEventType.REJECTED,
    ): TaskStatus.REJECTED,
    (
        TaskStatus.AWAITING_CONFIRMATION,
        TaskEventType.CANCELLED,
    ): TaskStatus.CANCELLED,
    (TaskStatus.SCHEDULED, TaskEventType.DUE): TaskStatus.DUE,
    (TaskStatus.SCHEDULED, TaskEventType.CANCELLED): TaskStatus.CANCELLED,
    (TaskStatus.DUE, TaskEventType.PENDING_DELIVERY): TaskStatus.PENDING_DELIVERY,
    (TaskStatus.DUE, TaskEventType.DELIVERING): TaskStatus.DELIVERING,
    (TaskStatus.DUE, TaskEventType.EXPIRED): TaskStatus.EXPIRED,
    (TaskStatus.DUE, TaskEventType.FAILED): TaskStatus.FAILED,
    (
        TaskStatus.PENDING_DELIVERY,
        TaskEventType.DELIVERING,
    ): TaskStatus.DELIVERING,
    (TaskStatus.PENDING_DELIVERY, TaskEventType.EXPIRED): TaskStatus.EXPIRED,
    (TaskStatus.DELIVERING, TaskEventType.DELIVERED): TaskStatus.DELIVERED,
    (TaskStatus.DELIVERING, TaskEventType.FAILED): TaskStatus.FAILED,
    (TaskStatus.DELIVERED, TaskEventType.ACKNOWLEDGED): TaskStatus.ACKNOWLEDGED,
    (TaskStatus.DELIVERED, TaskEventType.REJECTED): TaskStatus.REJECTED,
    (TaskStatus.DELIVERED, TaskEventType.EXPIRED): TaskStatus.EXPIRED,
}


def transition(current: TaskStatus, event: TaskEventType) -> TaskStatus:
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError as exc:
        raise InvalidTaskTransition(
            f"cannot apply {event.value} to {current.value}"
        ) from exc
