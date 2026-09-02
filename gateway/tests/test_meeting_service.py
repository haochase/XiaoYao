from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.meeting.briefing import BriefingResult
from companion_gateway.meeting.context import MeetingContextStore
from companion_gateway.meeting.models import MeetingEvent
from companion_gateway.meeting.service import MeetingReminderService
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def meeting(
    *,
    minutes: float = 10,
    status: str = "confirmed",
    rsvp: str = "accept",
    is_all_day: bool = False,
) -> MeetingEvent:
    start_at = NOW + timedelta(minutes=minutes)
    return MeetingEvent(
        fingerprint=FINGERPRINT,
        summary="产品周会",
        description_excerpt="同步迭代风险",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        location="3A会议室",
        status=status,
        rsvp_status=rsvp,
        is_all_day=is_all_day,
    )


class RecordingCalendar:
    def __init__(
        self,
        events: tuple[MeetingEvent, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.calls: list[tuple[str, datetime, datetime]] = []

    def list_upcoming(
        self,
        *,
        owner_open_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MeetingEvent, ...]:
        self.calls.append((owner_open_id, start_at, end_at))
        if self.error is not None:
            raise self.error
        return self.events


class RecordingBriefing:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[MeetingEvent, datetime]] = []

    def generate(self, event: MeetingEvent, *, now: datetime) -> BriefingResult:
        self.calls.append((event, now))
        return BriefingResult(self.text, "ai")


def build_service(
    tmp_path,
    calendar: RecordingCalendar,
    briefing: RecordingBriefing,
    *,
    database_path=None,
):
    repository = SQLiteTaskRepository(database_path or tmp_path / "tasks.db")
    repository.initialize()
    sequence = count(1)
    task_service = TaskService(
        repository,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}-meeting-{next(sequence)}",
    )
    context = MeetingContextStore(ttl_seconds=300)
    service = MeetingReminderService(
        calendar=calendar,
        context=context,
        briefing=briefing,
        task_service=task_service,
        task_executor=TaskExecutor(task_service),
        owner_open_id="ou-owner",
        target_device_id="desk-device",
    )
    return service, repository, context


def test_tick_creates_one_ai_meeting_task_and_refreshes_context(tmp_path) -> None:
    calendar = RecordingCalendar((meeting(),))
    briefing = RecordingBriefing("准备产品周会")
    service, repository, context = build_service(tmp_path, calendar, briefing)

    first = service.tick(now=NOW, trace_id="trc-1")
    second = service.tick(now=NOW + timedelta(seconds=30), trace_id="trc-2")

    assert first.fetched_count == 1
    assert first.candidate_count == 1
    assert len(first.created_task_ids) == 1
    assert second.created_task_ids == ()
    assert len(briefing.calls) == 1
    task = repository.get_task(first.created_task_ids[0])
    assert task is not None
    assert task.kind is TaskKind.MEETING_REMINDER
    assert task.schedule is not None
    assert task.schedule.at == NOW.astimezone(UTC)
    assert task.schedule.timezone == "Asia/Shanghai"
    assert task.confirmation_policy is ConfirmationPolicy.NONE
    assert task.payload.text == "准备产品周会"
    assert context.next_meeting(now=NOW + timedelta(seconds=30)) == meeting()


@pytest.mark.parametrize(
    ("status", "rsvp", "is_all_day"),
    [
        ("cancelled", "accept", False),
        ("confirmed", "decline", False),
        ("confirmed", "removed", False),
        ("confirmed", "accept", True),
    ],
)
def test_tick_filters_ineligible_events(
    status, rsvp, is_all_day, tmp_path
) -> None:
    service, repository, _ = build_service(
        tmp_path,
        RecordingCalendar(
            (meeting(status=status, rsvp=rsvp, is_all_day=is_all_day),)
        ),
        RecordingBriefing("ignored"),
    )

    result = service.tick(now=NOW, trace_id="trc-filter")

    assert result.created_task_ids == ()
    assert result.candidate_count == 0
    assert repository.list_due_tasks(now=NOW + timedelta(hours=1)) == []


def test_tick_includes_a_meeting_exactly_ten_minutes_away(tmp_path) -> None:
    service, _, _ = build_service(
        tmp_path,
        RecordingCalendar((meeting(minutes=10),)),
        RecordingBriefing("ten-minute reminder"),
    )

    result = service.tick(now=NOW, trace_id="trc-boundary")

    assert len(result.created_task_ids) == 1


def test_tick_treats_unknown_rsvp_as_eligible(tmp_path) -> None:
    service, _, _ = build_service(
        tmp_path,
        RecordingCalendar((meeting(rsvp="unknown"),)),
        RecordingBriefing("unknown-rsvp reminder"),
    )

    result = service.tick(now=NOW, trace_id="trc-unknown-rsvp")

    assert len(result.created_task_ids) == 1


def test_tick_excludes_a_meeting_beyond_the_lead_window(tmp_path) -> None:
    briefing = RecordingBriefing("too early")
    service, _, _ = build_service(
        tmp_path,
        RecordingCalendar((meeting(minutes=10 + 1 / 60),)),
        briefing,
    )

    result = service.tick(now=NOW, trace_id="trc-future")

    assert result.candidate_count == 0
    assert result.created_task_ids == ()
    assert briefing.calls == []


def test_tick_recovers_late_while_the_meeting_has_not_started(tmp_path) -> None:
    event = meeting(minutes=11)
    calendar = RecordingCalendar((event,))
    briefing = RecordingBriefing("late recovery")
    service, _, _ = build_service(tmp_path, calendar, briefing)

    first = service.tick(now=NOW, trace_id="trc-too-early")
    second = service.tick(
        now=NOW + timedelta(minutes=2),
        trace_id="trc-late-recovery",
    )

    assert first.created_task_ids == ()
    assert len(second.created_task_ids) == 1
    assert len(briefing.calls) == 1


@pytest.mark.parametrize("minutes", [0, -1])
def test_tick_excludes_meetings_that_already_started(minutes, tmp_path) -> None:
    briefing = RecordingBriefing("too late")
    service, _, _ = build_service(
        tmp_path,
        RecordingCalendar((meeting(minutes=minutes),)),
        briefing,
    )

    result = service.tick(now=NOW, trace_id="trc-started")

    assert result.created_task_ids == ()
    assert briefing.calls == []


def test_calendar_failure_preserves_context_without_creating_from_it(tmp_path) -> None:
    stale_event = meeting(minutes=5)
    calendar = RecordingCalendar((), error=RuntimeError("calendar unavailable"))
    briefing = RecordingBriefing("must not be called")
    service, repository, context = build_service(tmp_path, calendar, briefing)
    context.replace((stale_event,), refreshed_at=NOW)

    with pytest.raises(RuntimeError, match="calendar unavailable"):
        service.tick(now=NOW + timedelta(seconds=30), trace_id="trc-failure")

    assert context.next_meeting(now=NOW + timedelta(seconds=30)) == stale_event
    assert repository.list_due_tasks(now=NOW + timedelta(hours=1)) == []
    assert briefing.calls == []


def test_persisted_idempotency_key_prevents_ai_call_after_restart(tmp_path) -> None:
    event = meeting()
    database_path = tmp_path / "tasks.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()
    key = f"meeting:{event.fingerprint[:32]}:{int(event.start_at.timestamp())}"
    repository.create_task(
        TaskCreate(
            actor_id="feishu-calendar-user",
            target_device_id="desk-device",
            kind=TaskKind.MEETING_REMINDER,
            schedule=TaskSchedule(at=NOW, timezone="Asia/Shanghai"),
            payload=TaskPayload(text="already persisted"),
            confirmation_policy=ConfirmationPolicy.NONE,
            idempotency_key=key,
        ),
        task_id="tsk-persisted",
        event_id="evt-persisted",
        trace_id="trc-persisted",
        occurred_at=NOW,
    )
    briefing = RecordingBriefing("must not be generated")
    service, reopened_repository, _ = build_service(
        tmp_path,
        RecordingCalendar((event,)),
        briefing,
        database_path=database_path,
    )

    result = service.tick(now=NOW, trace_id="trc-restarted")

    assert result.candidate_count == 1
    assert result.created_task_ids == ()
    assert briefing.calls == []
    assert reopened_repository.get_task_by_idempotency_key(key) is not None
