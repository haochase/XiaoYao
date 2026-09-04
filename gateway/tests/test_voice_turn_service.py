from __future__ import annotations

import asyncio
import struct
import wave
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread

import pytest

from companion_gateway.audio.bridge import (
    AudioBridge,
    Pcm16Mono,
    resample_pcm16_mono,
)
from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.memory import MemoryCategory, MemoryProposalCandidate
from companion_gateway.domain.medication import MedicationOccurrenceStatus
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskRecord,
    TaskSchedule,
)
from companion_gateway.domain.tasks import TaskStatus
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository
from companion_gateway.memory.service import MemoryService
from companion_gateway.voice.delivery import DeviceVoiceDeliveryService
from companion_gateway.voice.runtime import (
    FakeModelRuntime,
    ModelResponse,
    VoiceAction,
    VoiceIntent,
)
from companion_gateway.voice.service import VoiceTurnService


def pcm_frame(*, sample_rate: int, sample_count: int, start: int = 0) -> Pcm16Mono:
    samples = [start + index for index in range(sample_count)]
    return Pcm16Mono(
        sample_rate=sample_rate,
        payload=struct.pack(f"<{sample_count}h", *samples),
    )


class EchoOpusCodec:
    def __init__(self, decoded: Pcm16Mono) -> None:
        self.decoded = decoded
        self.downlink_pcm: list[Pcm16Mono] = []

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return self.decoded

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        self.downlink_pcm.append(pcm)
        return b"opus-reply"


class SequenceOpusCodec(EchoOpusCodec):
    def __init__(self, decoded_frames: list[Pcm16Mono]) -> None:
        super().__init__(decoded_frames[0])
        self._decoded_frames = iter(decoded_frames)

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return next(self._decoded_frames)


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[bytes, ...]]] = []
        self.notification_messages: list[tuple[str, tuple[bytes, ...]]] = []
        self.notification_completion: Future[None] | None = None

    def send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None:
        self.messages.append((session_id, opus_frames))

    def send_notification_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> Future[None]:
        self.notification_messages.append((session_id, opus_frames))
        completion = self.notification_completion or Future()
        if self.notification_completion is None:
            completion.set_result(None)
        return completion


class TaskRuntime(FakeModelRuntime):
    def __init__(self, task: TaskCreate, response_pcm: Pcm16Mono) -> None:
        super().__init__(
            response_text="I scheduled that.",
            response_pcm=response_pcm,
        )
        self._task = task

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received_inputs.append(pcm)
        return ModelResponse(
            text="I scheduled that.",
            pcm=self._response.pcm,
            task=self._task,
        )


class MedicationActionRuntime(FakeModelRuntime):
    def __init__(self, response_pcm: Pcm16Mono, action: VoiceAction) -> None:
        super().__init__(response_text="好的，已记录。", response_pcm=response_pcm)
        self._action = action

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received_inputs.append(pcm)
        return ModelResponse(
            text="好的，已记录。",
            pcm=self._response.pcm,
            action=self._action,
        )


class RecordingMedicationService:
    def __init__(self) -> None:
        self.acknowledged: list[tuple[str, str, str]] = []

    def acknowledge_occurrence(self, occurrence_id: str, **kwargs):
        self.acknowledged.append(
            (occurrence_id, kwargs["actor_id"], kwargs["target_device_id"])
        )
        return type(
            "OccurrenceResult",
            (),
            {"status": MedicationOccurrenceStatus.ACKNOWLEDGED},
        )()

    def disable_plan(self, plan_id: str, **kwargs):
        return None


class MemoryProposalRuntime(FakeModelRuntime):
    def __init__(self, response_pcm: Pcm16Mono) -> None:
        super().__init__(response_text="好的", response_pcm=response_pcm)
        self.contexts: list[str] = []

    def set_memory_context(self, context: str) -> None:
        self.contexts.append(context)

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received_inputs.append(pcm)
        return ModelResponse(
            text="好的",
            pcm=self._response.pcm,
            memory_proposals=(
                MemoryProposalCandidate(
                    category=MemoryCategory.ADDRESS,
                    value="Call me Chase",
                ),
            ),
        )


class FailingMemoryService:
    def build_context(self, *, subject_id: str) -> str:
        return ""

    def propose(self, **kwargs):
        raise RuntimeError("storage unavailable")


class IntentRuntime:
    def __init__(self, intent: VoiceIntent, response_pcm: Pcm16Mono) -> None:
        self._intent = intent
        self._response_pcm = response_pcm
        self.received_inputs: list[Pcm16Mono] = []
        self.synthesized_texts: list[str] = []

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received_inputs.append(pcm)
        return ModelResponse(
            text="模型自由回复不应被使用",
            pcm=None,
            intent=self._intent,
        )

    def synthesize(self, text: str) -> Pcm16Mono:
        self.synthesized_texts.append(text)
        return self._response_pcm


class LatestReminderTaskService:
    def __init__(self, task: TaskRecord | None) -> None:
        self._task = task
        self.calls: list[tuple[str, str]] = []

    def get_latest_reminder(
        self,
        *,
        actor_id: str,
        target_device_id: str,
    ) -> TaskRecord | None:
        self.calls.append((actor_id, target_device_id))
        return self._task


def test_voice_turn_consumes_input_and_returns_companion_opus_reply() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=-400)
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=response_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.response_text == "我在这里，慢慢说。"
    assert turn.device_opus_frame == b"opus-reply"
    assert runtime.received_inputs == [input_pcm]
    assert codec.downlink_pcm[0].sample_rate == 24_000
    assert codec.downlink_pcm[0].sample_count == 1_440
    assert service.process_next_input() is None


@pytest.mark.parametrize(
    ("intent_type", "expected_text"),
    [
        ("current_time", "现在是12点34分。"),
        ("current_date", "今天是2026年8月13日。"),
        ("current_datetime", "现在是2026年8月13日12点34分。"),
    ],
)
def test_voice_turn_generates_deterministic_clock_intent_reply(
    intent_type: str,
    expected_text: str,
) -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(
        codec=codec,
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(VoiceIntent(type=intent_type), response_pcm)
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        clock=lambda: datetime(2026, 8, 13, 4, 34, tzinfo=UTC),
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input(target_device_id="living-room")

    assert turn is not None
    assert turn.response_text == expected_text
    assert runtime.synthesized_texts == [expected_text]


def test_voice_turn_generates_latest_reminder_status_reply() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(
        codec=codec,
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(VoiceIntent(type="reminder_status"), response_pcm)
    reminder = TaskRecord.model_validate(
        {
            "task_id": "tsk-latest",
            "actor_id": "family-1",
            "target_device_id": "living-room",
            "kind": "reminder",
            "schedule": {
                "at": "2026-08-13T20:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
            "payload": {"text": "按时服药"},
            "confirmation_policy": "required",
            "idempotency_key": "voice:latest",
            "status": TaskStatus.ACKNOWLEDGED,
            "created_at": "2026-08-13T04:00:00+00:00",
            "trace_id": "trc-latest",
        }
    )
    task_service = LatestReminderTaskService(reminder)
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        task_service=task_service,
        actor_id="family-1",
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input(target_device_id="living-room")

    assert turn is not None
    assert turn.response_text == "最近的提醒“按时服药”当前状态是已确认。"
    assert runtime.synthesized_texts == [turn.response_text]
    assert task_service.calls == [("family-1", "living-room")]


def test_voice_turn_reports_when_no_reminder_exists() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(
        codec=codec,
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(VoiceIntent(type="reminder_status"), response_pcm)
    task_service = LatestReminderTaskService(None)
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        task_service=task_service,
        actor_id="family-1",
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input(target_device_id="living-room")

    assert turn is not None
    assert turn.response_text == "目前没有找到提醒。"
    assert runtime.synthesized_texts == [turn.response_text]


def test_voice_turn_processes_all_pending_frames_as_one_turn() -> None:
    input_frames = [
        pcm_frame(sample_rate=16_000, sample_count=2, start=10),
        pcm_frame(sample_rate=16_000, sample_count=2, start=20),
        pcm_frame(sample_rate=16_000, sample_count=2, start=30),
    ]
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = SequenceOpusCodec(input_frames)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=4)
    runtime = FakeModelRuntime(
        response_text="鎴戝湪杩欓噷锛屾參鎱㈣銆?",
        response_pcm=response_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)

    for index in range(3):
        bridge.decode_uplink(f"input-opus-{index}".encode())

    turn = service.process_pending_turn()

    assert turn is not None
    assert len(runtime.received_inputs) == 1
    assert runtime.received_inputs[0].sample_rate == 16_000
    assert runtime.received_inputs[0].payload == (
        b"\n\x00\x0b\x00\x14\x00\x15\x00\x1e\x00\x1f\x00"
    )
    assert service.process_pending_turn() is None


def test_voice_turn_can_discard_pending_audio() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=2)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=2)
    runtime = FakeModelRuntime(
        response_text="鎴戝湪杩欓噷锛屾參鎱㈣銆?",
        response_pcm=pcm_frame(sample_rate=16_000, sample_count=960),
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    bridge.decode_uplink(b"stale-opus")

    service.clear_pending_input()

    assert service.process_pending_turn() is None
    assert runtime.received_inputs == []


def test_voice_turn_executes_a_validated_model_task(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "voice-task.db")
    repository.initialize()
    executor = TaskExecutor(TaskService(repository))
    command = TaskCreate(
        actor_id="voice-user",
        target_device_id="living-room",
        kind=TaskKind.REMINDER,
        schedule=TaskSchedule(
            at="2026-08-07T20:00:00+08:00",
            timezone="Asia/Shanghai",
        ),
        payload=TaskPayload(text="take medicine"),
        confirmation_policy=ConfirmationPolicy.REQUIRED,
        idempotency_key="voice:turn:task",
    )
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = TaskRuntime(
        command,
        pcm_frame(sample_rate=16_000, sample_count=960, start=100),
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        task_executor=executor,
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.task is not None
    assert turn.task.status.value == "awaiting_confirmation"
    assert (
        repository.get_task(turn.task.task_id).status.value
        == "awaiting_confirmation"
    )


def test_voice_turn_routes_medication_ack_action_through_gateway_policy() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    medication_service = RecordingMedicationService()
    runtime = MedicationActionRuntime(
        response_pcm,
        VoiceAction(
            type="acknowledge_medication_occurrence",
            occurrence_id="med-occurrence-1",
        ),
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        medication_service=medication_service,
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input(target_device_id="living-room")

    assert turn is not None
    assert medication_service.acknowledged == [
        ("med-occurrence-1", "voice-user", "living-room")
    ]


def test_voice_turn_stores_model_proposals_after_reply_and_sets_context(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "voice-memory.db")
    repository.initialize()
    ids = iter(["prop-1", "mem-1"])
    memory_service = MemoryService(
        repository,
        enabled=True,
        clock=lambda: datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        id_factory=lambda prefix: next(ids),
    )
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = MemoryProposalRuntime(
        pcm_frame(sample_rate=16_000, sample_count=960, start=100),
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        memory_service=memory_service,
        actor_id="family-1",
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.memory_proposal_ids == ("prop-1",)
    assert runtime.contexts == [""]
    assert memory_service.list_proposals(subject_id="family-1")[0].value == (
        "Call me Chase"
    )
    assert repository.export_memories(
        subject_id="family-1",
        now=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
    ) == []


def test_voice_turn_keeps_audio_when_memory_proposal_storage_fails() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = MemoryProposalRuntime(
        pcm_frame(sample_rate=16_000, sample_count=960, start=100),
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        memory_service=FailingMemoryService(),
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.device_opus_frame == b"opus-reply"
    assert turn.memory_proposal_ids == ()


def test_voice_turn_splits_a_long_response_into_60ms_opus_frames() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=-400)
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=2_400, start=100)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我会一直在这里陪着你。",
        response_pcm=response_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    bridge.decode_uplink(b"input-opus")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.device_opus_frames == (b"opus-reply",) * 3
    assert [frame.sample_count for frame in codec.downlink_pcm] == [1_440] * 3


def test_voice_turn_returns_a_real_opus_companion_reply() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "audio"
        / "companion-greeting-zh-cn.wav"
    )
    with wave.open(str(fixture_path), "rb") as source:
        fixture_pcm = Pcm16Mono(
            sample_rate=source.getframerate(),
            payload=source.readframes(960),
        )

    codec = PyAvOpusCodec()
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=fixture_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    uplink_packet = codec.encode_downlink(
        resample_pcm16_mono(fixture_pcm, target_sample_rate=24_000)
    )
    bridge.decode_uplink(uplink_packet)

    turn = service.process_next_input()

    assert turn is not None
    assert turn.response_text == "我在这里，慢慢说。"
    assert 0 < len(turn.device_opus_frame) <= 4_096
    replayed_reply = codec.decode_uplink(turn.device_opus_frame)
    assert replayed_reply.sample_rate == 16_000
    assert replayed_reply.sample_count == 960


def test_full_companion_fixture_becomes_a_90_frame_opus_stream() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "audio"
        / "companion-greeting-zh-cn.wav"
    )
    with wave.open(str(fixture_path), "rb") as source:
        fixture_payload = source.readframes(source.getnframes())
        sample_rate = source.getframerate()
    response_pcm = Pcm16Mono(
        sample_rate=sample_rate,
        payload=fixture_payload,
    )
    input_pcm = Pcm16Mono(sample_rate=sample_rate, payload=fixture_payload[:1920])
    codec = PyAvOpusCodec()
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=response_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    uplink_packet = codec.encode_downlink(
        resample_pcm16_mono(input_pcm, target_sample_rate=24_000)
    )
    bridge.decode_uplink(uplink_packet)

    turn = service.process_next_input()

    assert turn is not None
    assert len(turn.device_opus_frames) == 90
    assert all(0 < len(frame) <= 4_096 for frame in turn.device_opus_frames)


def test_device_voice_delivery_sends_processed_turn_to_active_session() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=-400)
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=response_pcm,
    )
    voice_turns = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )
    bridge.decode_uplink(b"input-opus")

    turn = delivery.process_and_send(session_id="ses-active")

    assert turn is not None
    assert transport.messages == [("ses-active", (b"opus-reply",))]


def test_device_voice_delivery_synthesizes_and_sends_reminder_text() -> None:
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(pcm_frame(sample_rate=16_000, sample_count=960))
    voice_turns = VoiceTurnService(
        audio_bridge=AudioBridge(
            codec=codec,
            model_sample_rate=16_000,
            queue_capacity=1,
        ),
        model_runtime=FakeModelRuntime(
            response_text="reply",
            response_pcm=response_pcm,
        ),
    )
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )

    delivery.synthesize_and_send(session_id="ses-reminder", text="take medicine")

    assert transport.messages == [("ses-reminder", (b"opus-reply",))]


def test_device_voice_delivery_marks_notification_text() -> None:
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=AudioBridge(
                codec=EchoOpusCodec(
                    pcm_frame(sample_rate=16_000, sample_count=960)
                ),
                model_sample_rate=16_000,
                queue_capacity=1,
            ),
            model_runtime=FakeModelRuntime(
                response_text="reply",
                response_pcm=response_pcm,
            ),
        ),
        device_transport=transport,
    )

    delivery.synthesize_notification_and_send(
        session_id="ses-notification",
        text="take medicine",
    )

    assert transport.notification_messages == [
        ("ses-notification", (b"opus-reply",))
    ]


def test_device_voice_delivery_waits_for_notification_completion() -> None:
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    transport = RecordingTransport()
    transport.notification_completion = Future()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=AudioBridge(
                codec=EchoOpusCodec(
                    pcm_frame(sample_rate=16_000, sample_count=960)
                ),
                model_sample_rate=16_000,
                queue_capacity=1,
            ),
            model_runtime=FakeModelRuntime(
                response_text="reply",
                response_pcm=response_pcm,
            ),
        ),
        device_transport=transport,
    )
    worker = Thread(
        target=delivery.synthesize_notification_and_send,
        kwargs={"session_id": "ses-notification", "text": "take medicine"},
    )

    worker.start()
    worker.join(timeout=0.05)
    assert worker.is_alive() is True
    transport.notification_completion.set_result(None)
    worker.join(timeout=1)

    assert worker.is_alive() is False


def test_device_voice_delivery_cancels_timed_out_notification() -> None:
    class ImmediateTimeoutFuture(Future[None]):
        def result(self, timeout=None):
            raise FutureTimeoutError()

    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    transport = RecordingTransport()
    completion: Future[None] = ImmediateTimeoutFuture()
    transport.notification_completion = completion
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=AudioBridge(
                codec=EchoOpusCodec(
                    pcm_frame(sample_rate=16_000, sample_count=960)
                ),
                model_sample_rate=16_000,
                queue_capacity=1,
            ),
            model_runtime=FakeModelRuntime(
                response_text="reply",
                response_pcm=response_pcm,
            ),
        ),
        device_transport=transport,
    )

    with pytest.raises(RuntimeError, match="notification_delivery_timeout"):
        delivery.synthesize_notification_and_send(
            session_id="ses-timeout",
            text="take medicine",
        )

    assert completion.cancelled() is True


def test_device_voice_delivery_runs_tts_canary_without_sending() -> None:
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(pcm_frame(sample_rate=16_000, sample_count=960))
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=AudioBridge(
                codec=codec,
                model_sample_rate=16_000,
                queue_capacity=1,
            ),
            model_runtime=FakeModelRuntime(
                response_text="reply",
                response_pcm=response_pcm,
            ),
        ),
        device_transport=transport,
    )

    assert delivery.can_synthesize("小瑶在线检查") is True
    assert transport.messages == []


def test_device_voice_delivery_propagates_memory_service(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "delivery-memory.db")
    repository.initialize()
    memory_service = MemoryService(
        repository,
        enabled=True,
        clock=lambda: datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        id_factory=lambda prefix: "prop-delivery",
    )
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = MemoryProposalRuntime(
        pcm_frame(sample_rate=16_000, sample_count=960, start=100),
    )
    voice_turns = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )
    delivery.set_memory_service(memory_service)
    bridge.decode_uplink(b"input-opus")

    turn = delivery.process_and_send(session_id="ses-memory")

    assert turn is not None
    assert turn.memory_proposal_ids == ("prop-delivery",)


def test_device_voice_delivery_keeps_pending_audio_isolated_by_session() -> None:
    first_input = pcm_frame(sample_rate=16_000, sample_count=2, start=10)
    second_input = pcm_frame(sample_rate=16_000, sample_count=2, start=20)
    codec = SequenceOpusCodec([first_input, second_input])
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=2)
    runtime = FakeModelRuntime(
        response_text="reply",
        response_pcm=pcm_frame(sample_rate=16_000, sample_count=960, start=100),
    )
    voice_turns = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )

    delivery.accept_and_send(session_id="ses-first", opus_frame=b"first")
    delivery.accept_and_send(session_id="ses-second", opus_frame=b"second")

    turn = delivery.process_and_send(session_id="ses-second")

    assert turn is not None
    assert runtime.received_inputs == [second_input]
    assert transport.messages == [("ses-second", (b"opus-reply",))]

    delivery.clear_pending_input(session_id="ses-first")

    assert delivery.process_and_send(session_id="ses-first") is None


def test_device_voice_delivery_can_process_off_event_loop() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=-400)
    response_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=100)
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="鎴戝湪杩欓噷锛屾參鎱㈣銆?",
        response_pcm=response_pcm,
    )
    voice_turns = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )
    bridge.decode_uplink(b"input-opus")

    turn = asyncio.run(delivery.process_and_send_async(session_id="ses-active"))

    assert turn is not None
    assert transport.messages == [("ses-active", (b"opus-reply",))]


def test_device_voice_delivery_splits_long_opus_response_across_streams() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960, start=-400)
    response_pcm = Pcm16Mono(
        sample_rate=16_000,
        payload=b"\x01\x00" * (960 * 130),
    )
    codec = EchoOpusCodec(input_pcm)
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)
    runtime = FakeModelRuntime(
        response_text="我会一直在这里陪着你。",
        response_pcm=response_pcm,
    )
    voice_turns = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    transport = RecordingTransport()
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=transport,
    )
    bridge.decode_uplink(b"input-opus")

    delivery.process_and_send(session_id="ses-active")

    assert [len(frames) for _, frames in transport.messages] == [128, 2]
def meeting(*, summary: str = "产品周会", minutes: int = 20, location: str = "3A会议室"):
    from companion_gateway.meeting.models import MeetingEvent

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    return MeetingEvent(
        fingerprint="a" * 64,
        summary=summary,
        description_excerpt="",
        start_at=now + __import__("datetime").timedelta(minutes=minutes),
        end_at=now + __import__("datetime").timedelta(minutes=minutes + 30),
        location=location,
        status="confirmed",
        rsvp_status="accept",
        is_all_day=False,
    )


def next_meeting_turn(
    *,
    meeting_context,
    clock=lambda: datetime(2026, 8, 27, 4, 0, tzinfo=UTC),
):
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(VoiceIntent(type="next_meeting"), response_pcm)
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        meeting_context=meeting_context,
        clock=clock,
    )
    bridge.decode_uplink(b"input-opus")
    return service.process_pending_turn(target_device_id="desk-device"), runtime


def test_next_meeting_intent_uses_fresh_calendar_facts() -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    context = MeetingContextStore(ttl_seconds=300)
    context.replace((meeting(),), refreshed_at=now)
    turn, runtime = next_meeting_turn(meeting_context=context)

    assert turn is not None
    assert turn.response_text == "下一场会议是产品周会，12点20分开始，地点是3A会议室。"
    assert runtime.synthesized_texts == [turn.response_text]


def test_next_meeting_intent_omits_empty_location_and_ignores_model_text() -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    context = MeetingContextStore(ttl_seconds=300)
    context.replace((meeting(location=""),), refreshed_at=now)
    turn, runtime = next_meeting_turn(meeting_context=context)

    assert turn is not None
    assert turn.response_text == "下一场会议是产品周会，12点20分开始。"
    assert "模型自由回复不应被使用" not in turn.response_text
    assert runtime.synthesized_texts == [turn.response_text]


def test_next_meeting_intent_reports_when_no_future_meeting_exists() -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    context = MeetingContextStore(ttl_seconds=300)
    context.replace((meeting(minutes=-1),), refreshed_at=now)
    turn, runtime = next_meeting_turn(meeting_context=context)

    assert turn is not None
    assert turn.response_text == "未来24小时没有查到会议。"
    assert runtime.synthesized_texts == [turn.response_text]


@pytest.mark.parametrize("context_kind", ("missing", "stale"))
def test_next_meeting_intent_reports_unavailable_without_fresh_context(
    context_kind: str,
) -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    context = None
    if context_kind == "stale":
        context = MeetingContextStore(ttl_seconds=300)
        context.replace((
            meeting(),
        ), refreshed_at=now - __import__("datetime").timedelta(seconds=301))
    turn, runtime = next_meeting_turn(meeting_context=context)

    assert turn is not None
    assert turn.response_text == "暂时无法读取飞书日历，请稍后再试。"
    assert runtime.synthesized_texts == [turn.response_text]


def test_next_meeting_intent_captures_the_clock_once() -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    context = MeetingContextStore(ttl_seconds=300)
    context.replace((meeting(),), refreshed_at=now)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return now

    turn, _ = next_meeting_turn(meeting_context=context, clock=clock)

    assert turn is not None
    assert calls == 1


def test_project_query_intent_uses_project_memory_sources() -> None:
    from companion_gateway.project.models import (
        DecisionCard,
        DecisionStatus,
        EvidenceRef,
        ProjectContextPackage,
    )
    from companion_gateway.project.service import ProjectMemoryService

    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    source = EvidenceRef(
        source_type="meeting_note",
        source_id="meeting-project-query",
        source_title="方案评审会",
        source_url="https://example.invalid/meeting-project-query",
        source_time=now,
        excerpt="会议决定采用方案 B。",
        permission_scope="project:star-retail",
    )
    project_decision = DecisionCard(
        decision_id="decision-project-query",
        project_id="project-project-query",
        topic="终端方案",
        decision_text="采用方案 B",
        rationale="交付风险更低",
        owner="owner-1",
        decided_at=now,
        source_refs=(source,),
        status=DecisionStatus.ACTIVE,
        confidence=0.92,
    )
    package = ProjectContextPackage(
        project_id="project-project-query",
        project_name="项目记忆测试项目",
        generated_at=now,
        source_refs=(source,),
        active_decisions=(project_decision,),
        permission_scope="project:star-retail",
    )
    project_memory = ProjectMemoryService(clock=lambda: now)
    project_memory.replace_context(package)

    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(
        VoiceIntent(type="project_query", query="终端方案"),
        response_pcm,
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        project_memory=project_memory,
        project_ids_by_device={"desk-device": "project-project-query"},
        clock=lambda: now,
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_pending_turn(target_device_id="desk-device")

    assert turn is not None
    assert turn.response_text == "当前有效决策：采用方案 B"

    bridge.decode_uplink(b"input-opus")
    unbound_turn = service.process_pending_turn(target_device_id="other-device")

    assert unbound_turn is not None
    assert unbound_turn.response_text == "暂时无法确认项目记忆，请稍后再试。"


def test_project_query_intent_fails_closed_without_project_memory() -> None:
    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(
        VoiceIntent(type="project_query", query="终端方案"),
        response_pcm,
    )
    service = VoiceTurnService(audio_bridge=bridge, model_runtime=runtime)
    bridge.decode_uplink(b"input-opus")

    turn = service.process_pending_turn(target_device_id="desk-device")

    assert turn is not None
    assert turn.response_text == "暂时无法确认项目记忆，请稍后再试。"


@pytest.mark.parametrize(
    ("error_label", "expected_text"),
    [
        ("source_stale", "相关项目资料已过期，请先同步。"),
        ("evidence_pending", "后台正在同步补充证据，请稍后再试。"),
    ],
)
def test_project_query_intent_maps_source_aware_failures(
    error_label: str,
    expected_text: str,
) -> None:
    from companion_gateway.project.service import ProjectContextUnavailable

    class UnavailableProjectMemory:
        def answer(self, *args, **kwargs):
            raise ProjectContextUnavailable(error_label)

    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(
        VoiceIntent(type="project_query", query="终端方案"),
        response_pcm,
    )
    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        project_memory=UnavailableProjectMemory(),
        project_ids_by_device={"desk-device": "project-1"},
    )
    bridge.decode_uplink(b"input-opus")

    turn = service.process_pending_turn(target_device_id="desk-device")

    assert turn is not None
    assert turn.response_text == expected_text


def test_device_voice_delivery_delegates_meeting_context() -> None:
    from companion_gateway.meeting.context import MeetingContextStore

    input_pcm = pcm_frame(sample_rate=16_000, sample_count=960)
    now = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
    response_pcm = pcm_frame(sample_rate=24_000, sample_count=1_440)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=1,
    )
    runtime = IntentRuntime(VoiceIntent(type="next_meeting"), response_pcm)
    voice_turns = VoiceTurnService(
        audio_bridge=bridge, model_runtime=runtime, clock=lambda: now
    )
    delivery = DeviceVoiceDeliveryService(
        voice_turn_service=voice_turns,
        device_transport=RecordingTransport(),
    )
    context = MeetingContextStore(ttl_seconds=300)
    context.replace((meeting(),), refreshed_at=now)

    delivery.set_meeting_context(context)
    bridge.decode_uplink(b"input-opus")
    turn = delivery.process_and_send(session_id="ses-meeting")

    assert turn is not None
    assert turn.response_text == "下一场会议是产品周会，12点20分开始，地点是3A会议室。"
