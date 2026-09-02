from datetime import UTC, datetime
import logging

import pytest

from companion_gateway.device.transport import (
    DeviceNotConnected,
    DeviceOutboundBackpressure,
)
from companion_gateway.domain.medication import FeishuSendResult
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskKind,
    TaskPayload,
    TaskRecord,
)
from companion_gateway.domain.tasks import TaskStatus
from companion_gateway.meeting.delivery import MeetingDeliveryService
from companion_gateway.voice.minicpm_o import ModelRuntimeError


REMINDER_TEXT = "10分钟后产品周会"


class RecordingSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class RecordingSessions:
    def __init__(self, session_id: str | None) -> None:
        self._session = (
            RecordingSession(session_id) if session_id is not None else None
        )

    def get(self, _device_id: str) -> RecordingSession | None:
        return self._session


class RecordingVoiceDelivery:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def synthesize_and_send(self, *, session_id: str, text: str) -> None:
        self.calls.append((session_id, text))
        if self._error is not None:
            raise self._error


class RecordingNotifier:
    def __init__(
        self,
        *,
        success: bool = True,
        error: Exception | None = None,
    ) -> None:
        self._success = success
        self._error = error
        self.calls: list[dict[str, str]] = []

    def send_text(self, *, text: str, trace_id: str) -> FeishuSendResult:
        self.calls.append({"text": text, "trace_id": trace_id})
        if self._error is not None:
            raise self._error
        return FeishuSendResult(
            success=self._success,
            message_id="om_meeting_delivery" if self._success else None,
            error=None if self._success else "provider_failure",
        )


def meeting_task(*, target_device_id: str = "meeting-device") -> TaskRecord:
    return TaskRecord.model_construct(
        task_id="task-meeting-1",
        actor_id="family-1",
        target_device_id=target_device_id,
        kind=TaskKind.MEETING_REMINDER,
        schedule=None,
        payload=TaskPayload(text=REMINDER_TEXT),
        confirmation_policy=ConfirmationPolicy.OPTIONAL,
        idempotency_key="meeting-delivery-1",
        status=TaskStatus.PENDING_DELIVERY,
        created_at=datetime(2026, 8, 27, 9, tzinfo=UTC),
        trace_id="trace-meeting-1",
    )


@pytest.mark.parametrize(
    "writeback_error",
    [None, RuntimeError("provider response should stay private")],
)
def test_online_device_uses_voice_and_writeback_failure_does_not_repeat(
    writeback_error: Exception | None,
) -> None:
    sessions = RecordingSessions(session_id="ses-1")
    voice = RecordingVoiceDelivery()
    notifier = RecordingNotifier(success=False, error=writeback_error)
    delivery = MeetingDeliveryService(sessions=sessions, voice=voice, notifier=notifier)

    result = delivery.deliver(meeting_task())

    assert result.delivered is True
    assert voice.calls == [("ses-1", REMINDER_TEXT)]
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["text"] == "桌面设备已完成会前提醒。"


def test_offline_device_uses_feishu_once_as_the_delivery_channel() -> None:
    notifier = RecordingNotifier()
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id=None),
        voice=RecordingVoiceDelivery(),
        notifier=notifier,
    )

    result = delivery.deliver(meeting_task())

    assert result.delivered is True
    assert notifier.calls == [{"text": REMINDER_TEXT, "trace_id": "trace-meeting-1"}]


def test_missing_voice_delivery_uses_feishu_fallback() -> None:
    notifier = RecordingNotifier()
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id="ses-1"),
        voice=None,
        notifier=notifier,
    )

    assert delivery.deliver(meeting_task()).delivered is True
    assert notifier.calls[0]["text"] == REMINDER_TEXT


@pytest.mark.parametrize(
    "voice_error",
    [
        DeviceNotConnected("device disconnected"),
        DeviceOutboundBackpressure("queue full"),
        ModelRuntimeError("model unavailable"),
        RuntimeError("runtime unavailable"),
        ValueError("invalid synthesis response"),
    ],
)
def test_tts_failures_use_feishu_fallback(voice_error: Exception) -> None:
    notifier = RecordingNotifier()
    voice = RecordingVoiceDelivery(error=voice_error)
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id="ses-1"),
        voice=voice,
        notifier=notifier,
    )

    result = delivery.deliver(meeting_task())

    assert result.delivered is True
    assert voice.calls == [("ses-1", REMINDER_TEXT)]
    assert notifier.calls == [{"text": REMINDER_TEXT, "trace_id": "trace-meeting-1"}]


@pytest.mark.parametrize(
    "notifier",
    [
        RecordingNotifier(success=False),
        RecordingNotifier(error=RuntimeError("transport response should stay private")),
    ],
)
def test_device_and_feishu_failure_remains_retryable(
    notifier: RecordingNotifier,
) -> None:
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id=None),
        voice=RecordingVoiceDelivery(),
        notifier=notifier,
    )

    result = delivery.deliver(meeting_task())

    assert result.delivered is False
    assert result.failure_reason == "feishu_fallback_failed"
    assert len(notifier.calls) == 1


def test_non_meeting_task_is_rejected() -> None:
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id=None),
        voice=RecordingVoiceDelivery(),
        notifier=RecordingNotifier(),
    )
    task = meeting_task().model_copy(update={"kind": TaskKind.REMINDER})

    with pytest.raises(ValueError, match="meeting delivery only accepts meeting reminders"):
        delivery.deliver(task)


def test_delivery_logs_are_redacted_and_exclude_sensitive_content(caplog) -> None:
    raw_device_id = "AA:BB:CC:DD:EE:FF"
    sensitive_error = "provider response body and credential"
    delivery = MeetingDeliveryService(
        sessions=RecordingSessions(session_id="ses-1"),
        voice=RecordingVoiceDelivery(error=RuntimeError(sensitive_error)),
        notifier=RecordingNotifier(success=False),
    )

    with caplog.at_level(
        logging.INFO,
        logger="companion_gateway.meeting.delivery",
    ):
        assert delivery.deliver(meeting_task(target_device_id=raw_device_id)).delivered is False

    messages = [record.getMessage() for record in caplog.records]
    joined = " ".join(messages)
    assert any("device=" in message and "task=task-meeting-1" in message for message in messages)
    assert raw_device_id not in joined
    assert REMINDER_TEXT not in joined
    assert sensitive_error not in joined
    assert "om_meeting_delivery" not in joined
    assert "error_type=RuntimeError" in joined
