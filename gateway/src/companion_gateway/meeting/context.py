from datetime import datetime, timedelta
from threading import RLock

from companion_gateway.meeting.models import (
    MeetingEvent,
    MeetingSnapshot,
    is_meeting_eligible,
)


class MeetingContextStore:
    def __init__(self, *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._snapshot: MeetingSnapshot | None = None
        self._lock = RLock()

    def replace(
        self, events: tuple[MeetingEvent, ...], *, refreshed_at: datetime
    ) -> None:
        if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
            raise ValueError("refreshed_at must be timezone-aware")
        snapshot = MeetingSnapshot(
            tuple(sorted(events, key=lambda item: item.start_at)), refreshed_at
        )
        with self._lock:
            self._snapshot = snapshot

    def is_fresh(self, *, now: datetime) -> bool:
        with self._lock:
            snapshot = self._snapshot
        return snapshot is not None and now - snapshot.refreshed_at <= self._ttl

    def next_meeting(self, *, now: datetime) -> MeetingEvent | None:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or now - snapshot.refreshed_at > self._ttl:
            return None
        return next(
            (
                event
                for event in snapshot.events
                if is_meeting_eligible(event, now=now)
            ),
            None,
        )

    def events(self, *, now: datetime) -> tuple[MeetingEvent, ...]:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or now - snapshot.refreshed_at > self._ttl:
            return ()
        return snapshot.events
