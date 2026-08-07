from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path

from companion_gateway.audio.bridge import (
    AudioBridge,
    Pcm16Mono,
    resample_pcm16_mono,
)
from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
from companion_gateway.service import TaskService
from companion_gateway.storage.sqlite import SQLiteTaskRepository
from companion_gateway.voice.delivery import DeviceVoiceDeliveryService
from companion_gateway.voice.runtime import FakeModelRuntime, ModelResponse
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

    def send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None:
        self.messages.append((session_id, opus_frames))


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
    assert turn.task.status.value == "scheduled"
    assert repository.get_task(turn.task.task_id).status.value == "scheduled"


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
