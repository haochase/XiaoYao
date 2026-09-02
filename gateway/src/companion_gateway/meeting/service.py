from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.meeting.briefing import MeetingBriefingService
from companion_gateway.meeting.context import MeetingContextStore
from companion_gateway.meeting.feishu import FeishuCalendarClient
from companion_gateway.meeting.models import is_meeting_eligible
from companion_gateway.service import TaskService


@dataclass(frozen=True)
class MeetingTickResult:
    fetched_count: int
    candidate_count: int
    created_task_ids: tuple[str, ...]


class MeetingReminderService:
    def __init__(
        self,
        *,
        calendar: FeishuCalendarClient,
        context: MeetingContextStore,
        briefing: MeetingBriefingService,
        task_service: TaskService,
        task_executor: TaskExecutor,
        owner_open_id: str,
        target_device_id: str,
        lookahead_hours: int = 24,
        lead_seconds: int = 600,
    ) -> None:
        self._calendar = calendar
        self._context = context
        self._briefing = briefing
        self._task_service = task_service
        self._task_executor = task_executor
        self._owner_open_id = owner_open_id
        self._target_device_id = target_device_id
        self._lookahead = timedelta(hours=lookahead_hours)
        self._lead = timedelta(seconds=lead_seconds)

    def tick(self, *, now: datetime, trace_id: str) -> MeetingTickResult:
        events = self._calendar.list_upcoming(
            owner_open_id=self._owner_open_id,
            start_at=now,
            end_at=now + self._lookahead,
        )
        self._context.replace(events, refreshed_at=now)
        candidates = tuple(
            event
            for event in events
            if is_meeting_eligible(event, now=now)
            and event.start_at - now <= self._lead
        )
        created_ids: list[str] = []
        for event in candidates:
            key = (
                f"meeting:{event.fingerprint[:32]}:"
                f"{int(event.start_at.timestamp())}"
            )
            if self._task_service.get_task_by_idempotency_key(key) is not None:
                continue
            result = self._briefing.generate(event, now=now)
            task, created = self._task_executor.create_and_schedule(
                TaskCreate(
                    actor_id="feishu-calendar-user",
                    target_device_id=self._target_device_id,
                    kind=TaskKind.MEETING_REMINDER,
                    schedule=TaskSchedule(
                        at=now.astimezone(UTC),
                        timezone="Asia/Shanghai",
                    ),
                    payload=TaskPayload(text=result.text),
                    confirmation_policy=ConfirmationPolicy.NONE,
                    idempotency_key=key,
                ),
                trace_id=trace_id,
            )
            if created:
                created_ids.append(task.task_id)
        return MeetingTickResult(
            fetched_count=len(events),
            candidate_count=len(candidates),
            created_task_ids=tuple(created_ids),
        )
