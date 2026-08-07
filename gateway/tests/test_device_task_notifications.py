import asyncio
from datetime import datetime

from companion_gateway.device.transport import (
    DeviceTransport,
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
