from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta
import json
from pathlib import Path

from companion_gateway.domain.memory import utc_now
from companion_gateway.meeting.briefing import MeetingBriefingService
from companion_gateway.meeting.feishu import FeishuCalendarClient
from companion_gateway.meeting.models import MeetingEvent, is_meeting_eligible
from companion_gateway.settings import Settings, load_environment_file


GATEWAY_ENV_PATH = Path(__file__).resolve().parents[1] / "gateway" / ".env"


def sanitized_event(event: MeetingEvent) -> dict[str, object]:
    return {
        "summary": event.summary,
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat(),
        "location": event.location,
        "status": event.status,
        "rsvp_status": event.rsvp_status,
        "is_all_day": event.is_all_day,
    }


def sanitized_dry_run_reminder(
    event: MeetingEvent,
    *,
    now: datetime,
) -> dict[str, object]:
    return {
        "summary": event.summary,
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat(),
        "location": event.location,
        "text": MeetingBriefingService.fallback_text(event, now=now),
    }


def run_check(
    *,
    settings: Settings,
    hours: int,
    client_factory=FeishuCalendarClient,
    clock: Callable[[], datetime] = utc_now,
    dry_run: bool = False,
) -> dict[str, object]:
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 72:
        raise ValueError("hours must be between 1 and 72")
    if not settings.feishu_configured:
        raise ValueError("Feishu calendar is not configured")
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    if (
        settings.feishu_app_id is None
        or settings.feishu_app_secret is None
        or settings.feishu_receiver_open_id is None
    ):
        raise ValueError("Feishu calendar settings are incomplete")
    user_token_state_path = settings.feishu_user_token_state_path
    if not user_token_state_path.is_absolute():
        user_token_state_path = GATEWAY_ENV_PATH.parent / user_token_state_path
    client = client_factory(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        base_url=settings.feishu_base_url,
        timeout_seconds=settings.feishu_timeout_seconds,
        max_retries=settings.feishu_max_retries,
        retry_backoff_seconds=settings.feishu_retry_backoff_seconds,
        owner_user_access_token=settings.feishu_owner_user_access_token,
        owner_refresh_token=settings.feishu_owner_refresh_token,
        owner_calendar_id=settings.feishu_owner_calendar_id,
        user_token_state_path=user_token_state_path,
    )
    events = client.list_upcoming(
        owner_open_id=settings.feishu_receiver_open_id,
        start_at=now,
        end_at=now + timedelta(hours=hours),
    )
    if dry_run:
        lead_seconds = getattr(settings, "meeting_reminder_lead_seconds", 600)
        reminders = [
            sanitized_dry_run_reminder(event, now=now)
            for event in events
            if is_meeting_eligible(event, now=now)
            and event.start_at - now <= timedelta(seconds=lead_seconds)
        ]
        return {
            "configured": True,
            "mode": "dry_run",
            "device": "offline",
            "event_count": len(events),
            "reminder_count": len(reminders),
            "reminders": reminders,
        }
    return {
        "configured": True,
        "event_count": len(events),
        "events": [sanitized_event(event) for event in events],
    }


class SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid command arguments")


def main(argv: list[str] | None = None) -> int:
    configured = False
    try:
        parser = SanitizedArgumentParser(
            description="Run a sanitized Feishu calendar read check."
        )
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print reminder candidates without ESP32 or Feishu delivery.",
        )
        args = parser.parse_args(argv)
        load_environment_file(GATEWAY_ENV_PATH)
        settings = Settings.from_environment()
        configured = settings.feishu_configured
        result = run_check(
            settings=settings,
            hours=args.hours,
            client_factory=FeishuCalendarClient,
            clock=utc_now,
            dry_run=args.dry_run,
        )
    except Exception:
        print(
            json.dumps(
                {
                    "configured": configured,
                    "event_count": 0,
                    "events": [],
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
