from __future__ import annotations

import struct
from dataclasses import dataclass

from companion_gateway.audio.bridge import AudioBridge, Pcm16Mono
from companion_gateway.domain.medication import MedicationOccurrenceStatus
from companion_gateway.voice.runtime import ModelResponse, VoiceAction
from companion_gateway.voice.service import VoiceTurnService


def pcm_frame(*, start: int = 0) -> Pcm16Mono:
    samples = [start + index for index in range(960)]
    return Pcm16Mono(
        sample_rate=16_000,
        payload=struct.pack("<960h", *samples),
    )


class EchoOpusCodec:
    def __init__(self, decoded: Pcm16Mono) -> None:
        self._decoded = decoded

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return self._decoded

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        return b"opus-reply"


@dataclass
class RecordingMedicationService:
    acknowledgements: list[tuple[str, str, str]]

    def acknowledge_occurrence(self, occurrence_id: str, **kwargs):
        self.acknowledgements.append(
            (occurrence_id, kwargs["actor_id"], kwargs["target_device_id"])
        )
        return type(
            "OccurrenceResult",
            (),
            {"status": MedicationOccurrenceStatus.ACKNOWLEDGED},
        )()

    def disable_plan(self, plan_id: str, **kwargs):
        raise AssertionError(f"unexpected plan disable: {plan_id}")


class AgentSessionRuntime:
    def __init__(self, response_pcm: Pcm16Mono) -> None:
        self.agent_contexts: list[str] = []
        self._responses = iter(
            [
                ModelResponse(
                    text="好的，已记录服药。",
                    pcm=response_pcm,
                    action=VoiceAction(
                        type="acknowledge_medication_occurrence",
                        occurrence_id="occurrence-1",
                    ),
                ),
                ModelResponse(
                    text="好的，稍后再提醒你。",
                    pcm=response_pcm,
                    action=None,
                ),
            ]
        )

    def set_agent_context(self, context: str) -> None:
        self.agent_contexts.append(context)

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        return next(self._responses)


def test_active_agent_session_keeps_two_turns_and_only_positive_action_acknowledges() -> None:
    input_pcm = pcm_frame()
    response_pcm = pcm_frame(start=100)
    bridge = AudioBridge(
        codec=EchoOpusCodec(input_pcm),
        model_sample_rate=16_000,
        queue_capacity=2,
    )
    runtime = AgentSessionRuntime(response_pcm)
    medication_service = RecordingMedicationService(acknowledgements=[])
    provider_calls: list[tuple[str, str | None]] = []
    contexts = {
        "living-room": "Gateway-owned active English practice for living-room.",
        "bedroom": "Gateway-owned active companion mode for bedroom.",
    }

    def agent_context_provider(actor_id: str, target_device_id: str | None) -> str:
        provider_calls.append((actor_id, target_device_id))
        return contexts.get(target_device_id or "", "")

    service = VoiceTurnService(
        audio_bridge=bridge,
        model_runtime=runtime,
        medication_service=medication_service,
        agent_context_provider=agent_context_provider,
    )

    service.accept_opus_uplink(b"first-turn", session_id="session-living")
    first = service.process_next_input(
        session_id="session-living",
        target_device_id="living-room",
    )
    service.accept_opus_uplink(b"second-turn", session_id="session-bedroom")
    second = service.process_next_input(
        session_id="session-bedroom",
        target_device_id="bedroom",
    )

    assert first is not None
    assert second is not None
    assert runtime.agent_contexts == [
        "Gateway-owned active English practice for living-room.",
        "Gateway-owned active companion mode for bedroom.",
    ]
    assert provider_calls == [
        ("voice-user", "living-room"),
        ("voice-user", "bedroom"),
    ]
    assert medication_service.acknowledgements == [
        ("occurrence-1", "voice-user", "living-room")
    ]
