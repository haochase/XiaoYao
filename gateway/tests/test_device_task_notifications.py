import asyncio
from datetime import datetime

import companion_gateway.device.transport as transport_module

from companion_gateway.device.transport import (
    DeviceTransport,
    OutboundControl,
    OutboundTask,
    OutboundTts,
)
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskKind,
    TaskPayload,
    TaskRecord,
    TaskSchedule,
)
from companion_gateway.domain.tasks import TaskStatus


def task_record() -> TaskRecord:
    return TaskRecord(
        task_id="tsk-notify-1",
        actor_id="voice-user",
        target_device_id="living-room",
        kind=TaskKind.REMINDER,
        schedule=TaskSchedule(
            at="2026-08-07T20:00:00+08:00",
            timezone="Asia/Shanghai",
        ),
        payload=TaskPayload(text="take medicine"),
        confirmation_policy=ConfirmationPolicy.REQUIRED,
        idempotency_key="voice:notify:1",
        status=TaskStatus.PENDING_DELIVERY,
        created_at=datetime.fromisoformat("2026-08-07T11:00:00+00:00"),
        trace_id="trace-create",
    )


def test_device_transport_delivers_task_notifications_separately_from_tts() -> None:
    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-notify")

        transport.send_task("ses-notify", task_record())

        message = await transport.next_task("ses-notify")

        assert message.session_id == "ses-notify"
        assert message.task.task_id == "tsk-notify-1"

    asyncio.run(scenario())


def test_device_transport_preserves_tts_and_task_when_both_are_ready() -> None:
    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-both")
        transport.send_tts("ses-both", b"tts-opus")
        transport.send_task("ses-both", task_record())

        first = await transport.next_outbound("ses-both")
        second = await asyncio.wait_for(
            transport.next_outbound("ses-both"),
            timeout=0.1,
        )

        assert {type(first), type(second)} == {OutboundTts, OutboundTask}

    asyncio.run(scenario())


def test_device_transport_marks_notification_tts() -> None:
    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-notification")

        transport.send_notification_tts_stream(
            "ses-notification",
            (b"notification-opus",),
        )

        message = await transport.next_tts("ses-notification")
        assert message.purpose == "notification"

    asyncio.run(scenario())


def test_device_transport_delivers_control_messages_through_outbound_queue() -> None:
    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-control")

        transport.send_control(
            "ses-control",
            {"type": "keepalive", "session_id": "ses-control"},
        )

        message = await transport.next_outbound("ses-control")
        assert isinstance(message, OutboundControl)
        assert message.payload == {
            "type": "keepalive",
            "session_id": "ses-control",
        }

    asyncio.run(scenario())


def test_cancelling_outbound_wait_cleans_up_both_internal_waiters(
    monkeypatch,
) -> None:
    created_waiters: list[asyncio.Task[object]] = []
    original_create_task = asyncio.create_task

    def capture_waiter(coroutine) -> asyncio.Task[object]:
        waiter = original_create_task(coroutine)
        created_waiters.append(waiter)
        return waiter

    monkeypatch.setattr(transport_module.asyncio, "create_task", capture_waiter)

    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-cancel")
        waiting = original_create_task(transport.next_outbound("ses-cancel"))
        await asyncio.sleep(0)

        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            pass

        await asyncio.sleep(0)
        assert len(created_waiters) == 3
        assert all(waiter.done() for waiter in created_waiters)
        assert all(waiter.cancelled() for waiter in created_waiters)

    asyncio.run(scenario())
