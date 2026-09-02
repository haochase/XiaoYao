from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.meeting.context import MeetingContextStore
from companion_gateway.meeting.models import MeetingEvent


NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)


def event(
    name: str,
    minutes: int,
    *,
    status: str = "confirmed",
    rsvp_status: str = "accept",
    is_all_day: bool = False,
) -> MeetingEvent:
    return MeetingEvent(
        fingerprint=(name.encode().hex() + "0" * 64)[:64],
        summary=name,
        description_excerpt="准备演示",
        start_at=NOW + timedelta(minutes=minutes),
        end_at=NOW + timedelta(minutes=minutes + 30),
        location="3A 会议室",
        status=status,
        rsvp_status=rsvp_status,
        is_all_day=is_all_day,
    )


def test_context_sorts_events_and_expires_after_five_minutes() -> None:
    store = MeetingContextStore(ttl_seconds=300)
    store.replace((event("later", 30), event("next", 10)), refreshed_at=NOW)

    assert store.next_meeting(now=NOW).summary == "next"
    assert store.is_fresh(now=NOW + timedelta(seconds=300)) is True
    assert store.is_fresh(now=NOW + timedelta(seconds=301)) is False
    assert store.next_meeting(now=NOW + timedelta(seconds=301)) is None


def test_next_meeting_excludes_an_already_started_event() -> None:
    store = MeetingContextStore(ttl_seconds=300)
    store.replace((event("started", -1), event("upcoming", 1)), refreshed_at=NOW)

    assert store.next_meeting(now=NOW).summary == "upcoming"
    assert store.next_meeting(now=NOW + timedelta(minutes=1)) is None


@pytest.mark.parametrize(
    "excluded",
    [
        event("all-day", 1, is_all_day=True),
        event("cancelled", 1, status="cancelled"),
        event("declined", 1, rsvp_status="decline"),
        event("removed", 1, rsvp_status="removed"),
    ],
)
def test_next_meeting_excludes_events_that_cannot_be_reminded(
    excluded: MeetingEvent,
) -> None:
    store = MeetingContextStore(ttl_seconds=300)
    eligible = event("eligible", 2)
    store.replace((excluded, eligible), refreshed_at=NOW)

    assert store.next_meeting(now=NOW) == eligible


def test_next_meeting_treats_unknown_rsvp_as_eligible() -> None:
    store = MeetingContextStore(ttl_seconds=300)
    unknown = event("unknown", 1, rsvp_status="unknown")
    store.replace((unknown,), refreshed_at=NOW)

    assert store.next_meeting(now=NOW) == unknown


def test_events_returns_sorted_fresh_snapshot_and_empty_when_expired() -> None:
    store = MeetingContextStore(ttl_seconds=300)
    store.replace((event("later", 30), event("first", 10)), refreshed_at=NOW)

    assert tuple(item.summary for item in store.events(now=NOW)) == ("first", "later")
    assert store.events(now=NOW + timedelta(seconds=301)) == ()


def test_context_rejects_non_positive_ttl_and_naive_refresh() -> None:
    with pytest.raises(ValueError, match="positive"):
        MeetingContextStore(ttl_seconds=0)

    store = MeetingContextStore(ttl_seconds=300)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.replace((event("next", 1),), refreshed_at=datetime(2026, 8, 27, 4, 0))


def test_context_replacements_are_safe_for_concurrent_readers() -> None:
    store = MeetingContextStore(ttl_seconds=300)

    def replace_and_read(index: int) -> str | None:
        refreshed_at = NOW + timedelta(seconds=index)
        store.replace(
            (event(f"meeting-{index}", index + 1),), refreshed_at=refreshed_at
        )
        current = store.events(now=refreshed_at)
        return current[0].summary if current else None

    with ThreadPoolExecutor(max_workers=8) as executor:
        observed = list(executor.map(replace_and_read, range(32)))

    assert all(value is not None for value in observed)
