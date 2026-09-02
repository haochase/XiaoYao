from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.meeting.models import MeetingEvent, MeetingSnapshot


NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)


def valid_event(**overrides: object) -> MeetingEvent:
    data: dict[str, object] = {
        "fingerprint": "a" * 64,
        "summary": "周会",
        "description_excerpt": "准备演示",
        "start_at": NOW + timedelta(minutes=10),
        "end_at": NOW + timedelta(minutes=40),
        "location": "3A 会议室",
        "status": "confirmed",
        "rsvp_status": "accept",
        "is_all_day": False,
    }
    data.update(overrides)
    return MeetingEvent(**data)


def test_meeting_event_accepts_sanitized_contract() -> None:
    event = valid_event()

    assert event.summary == "周会"
    assert event.status == "confirmed"
    assert event.rsvp_status == "accept"


def test_meeting_event_rejects_naive_or_reversed_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        valid_event(start_at=datetime(2026, 8, 27, 12, 0))

    with pytest.raises(ValueError, match="timezone-aware"):
        valid_event(end_at=datetime(2026, 8, 27, 13, 0))

    with pytest.raises(ValueError, match="later than start_at"):
        valid_event(end_at=NOW + timedelta(minutes=10))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fingerprint", "A" * 64, "lowercase sha256"),
        ("fingerprint", "a" * 63, "lowercase sha256"),
        ("summary", "   ", "1 to 1000"),
        ("summary", "x" * 1001, "1 to 1000"),
        ("description_excerpt", "x" * 1001, "1000"),
        ("location", "x" * 513, "512"),
    ],
)
def test_meeting_event_rejects_invalid_sanitized_fields(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        valid_event(**{field: value})


@pytest.mark.parametrize("status", ["confirmedx", "", None, 1])
def test_meeting_event_runtime_validates_status(status: object) -> None:
    with pytest.raises(ValueError, match="unsupported meeting status"):
        valid_event(status=status)


@pytest.mark.parametrize("rsvp_status", ["accepted", "", None, 1])
def test_meeting_event_runtime_validates_rsvp_status(rsvp_status: object) -> None:
    with pytest.raises(ValueError, match="unsupported RSVP status"):
        valid_event(rsvp_status=rsvp_status)


def test_meeting_models_are_immutable() -> None:
    event = valid_event()
    snapshot = MeetingSnapshot(events=(event,), refreshed_at=NOW)

    with pytest.raises(FrozenInstanceError):
        event.summary = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.events = ()  # type: ignore[misc]
