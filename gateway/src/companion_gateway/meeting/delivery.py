from __future__ import annotations

import logging

from companion_gateway.device.session import redact_device_id
from companion_gateway.device.transport import (
    DeviceNotConnected,
    DeviceOutboundBackpressure,
)
from companion_gateway.domain.executor import TaskDeliveryAttempt
from companion_gateway.domain.models import TaskKind, TaskRecord
from companion_gateway.voice.minicpm_o import ModelRuntimeError


logger = logging.getLogger(__name__)


class MeetingDeliveryService:
    def __init__(self, *, sessions, voice, notifier) -> None:
        self._sessions = sessions
        self._voice = voice
        self._notifier = notifier

    def deliver(self, task: TaskRecord) -> TaskDeliveryAttempt:
        if task.kind is not TaskKind.MEETING_REMINDER:
            raise ValueError("meeting delivery only accepts meeting reminders")

        device_id = redact_device_id(task.target_device_id)
        session = self._sessions.get(task.target_device_id)
        if session is not None and self._voice is not None:
            try:
                self._voice.synthesize_and_send(
                    session_id=session.session_id,
                    text=task.payload.text,
                )
            except (
                DeviceNotConnected,
                DeviceOutboundBackpressure,
                ModelRuntimeError,
                RuntimeError,
                ValueError,
            ) as exc:
                logger.info(
                    "meeting_voice_delivery_failed device=%s task=%s error_type=%s",
                    device_id,
                    task.task_id,
                    type(exc).__name__,
                )
            else:
                self._writeback_voice_success(task, device_id)
                return TaskDeliveryAttempt.succeeded()
        else:
            logger.info(
                "meeting_voice_delivery_unavailable device=%s task=%s",
                device_id,
                task.task_id,
            )

        return self._send_feishu_fallback(task, device_id)

    def _writeback_voice_success(self, task: TaskRecord, device_id: str) -> None:
        try:
            result = self._notifier.send_text(
                text="桌面设备已完成会前提醒。",
                trace_id=task.trace_id,
            )
        except Exception as exc:
            logger.info(
                "meeting_voice_writeback_failed device=%s task=%s error_type=%s",
                device_id,
                task.task_id,
                type(exc).__name__,
            )
            return
        if not result.success:
            logger.info(
                "meeting_voice_writeback_failed device=%s task=%s result=unsuccessful",
                device_id,
                task.task_id,
            )
            return
        logger.info(
            "meeting_voice_delivery_succeeded device=%s task=%s",
            device_id,
            task.task_id,
        )

    def _send_feishu_fallback(
        self,
        task: TaskRecord,
        device_id: str,
    ) -> TaskDeliveryAttempt:
        try:
            result = self._notifier.send_text(
                text=task.payload.text,
                trace_id=task.trace_id,
            )
        except Exception as exc:
            logger.info(
                "meeting_feishu_fallback_failed device=%s task=%s error_type=%s",
                device_id,
                task.task_id,
                type(exc).__name__,
            )
            return TaskDeliveryAttempt.failed("feishu_fallback_failed")
        if not result.success:
            logger.info(
                "meeting_feishu_fallback_failed device=%s task=%s result=unsuccessful",
                device_id,
                task.task_id,
            )
            return TaskDeliveryAttempt.failed("feishu_fallback_failed")
        logger.info(
            "meeting_feishu_fallback_succeeded device=%s task=%s",
            device_id,
            task.task_id,
        )
        return TaskDeliveryAttempt.succeeded()
