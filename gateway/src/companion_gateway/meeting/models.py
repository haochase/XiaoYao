from dataclasses import dataclass
from datetime import datetime
from typing import Literal


MeetingStatus = Literal["confirmed", "tentative", "cancelled"]
RsvpStatus = Literal[
    "accept", "tentative", "decline", "needs_action", "removed", "unknown"
]


@dataclass(frozen=True)
class MeetingEvent:
    fingerprint: str
    summary: str
    description_excerpt: str
    start_at: datetime
    end_at: datetime
    location: str
    status: MeetingStatus
    rsvp_status: RsvpStatus
    is_all_day: bool

    def __post_init__(self) -> None:
        if len(self.fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.fingerprint
        ):
            raise ValueError("fingerprint must be lowercase sha256")
        if not self.summary.strip() or len(self.summary) > 1000:
            raise ValueError("summary must contain 1 to 1000 characters")
        if len(self.description_excerpt) > 1000:
            raise ValueError("description_excerpt must not exceed 1000 characters")
        if len(self.location) > 512:
            raise ValueError("location must not exceed 512 characters")
        if not isinstance(self.status, str) or self.status not in {
            "confirmed",
            "tentative",
            "cancelled",
        }:
            raise ValueError("unsupported meeting status")
        if not isinstance(self.rsvp_status, str) or self.rsvp_status not in {
            "accept",
            "tentative",
            "decline",
            "needs_action",
            "removed",
            "unknown",
        }:
            raise ValueError("unsupported RSVP status")
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("start_at must be timezone-aware")
        if self.end_at.tzinfo is None or self.end_at.utcoffset() is None:
            raise ValueError("end_at must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")


def is_meeting_eligible(event: MeetingEvent, *, now: datetime) -> bool:
    return (
        event.start_at > now
        and not event.is_all_day
        and event.status != "cancelled"
        and event.rsvp_status not in {"decline", "removed"}
    )


@dataclass(frozen=True)
class MeetingSnapshot:
    events: tuple[MeetingEvent, ...]
    refreshed_at: datetime
