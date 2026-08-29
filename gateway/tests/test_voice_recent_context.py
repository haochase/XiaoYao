from __future__ import annotations

import struct

from companion_gateway.audio.bridge import AudioBridge, Pcm16Mono
from companion_gateway.voice.runtime import ModelResponse
from companion_gateway.voice.service import VoiceTurnService


def pcm_frame(start: int = 0) -> Pcm16Mono:
    return Pcm16Mono(
        sample_rate=16_000,
        payload=struct.pack("<960h", *(start + index for index in range(960))),
    )


class Codec:
    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return pcm_frame()

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        return b"opus"


class Runtime:
    def __init__(self) -> None:
        self.contexts: list[str] = []

    def set_recent_context(self, context: str) -> None:
        self.contexts.append(context)

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        return ModelResponse(text="收到", pcm=pcm_frame(100))


def test_voice_turn_reads_target_scoped_recent_context_and_clears_each_turn() -> None:
    runtime = Runtime()
    service = VoiceTurnService(
        audio_bridge=AudioBridge(
            codec=Codec(),
            model_sample_rate=16_000,
            queue_capacity=2,
        ),
        model_runtime=runtime,
        recent_context_provider=lambda actor, device: (
            f"context-{device}" if device == "living-room" else ""
        ),
    )

    service.accept_opus_uplink(b"one", session_id="session-1")
    service.process_next_input(session_id="session-1", target_device_id="living-room")
    service.accept_opus_uplink(b"two", session_id="session-2")
    service.process_next_input(session_id="session-2", target_device_id="bedroom")

    assert runtime.contexts == ["context-living-room", ""]


def test_voice_recent_context_provider_failure_isolated_from_reply() -> None:
    runtime = Runtime()

    def failing_provider(actor: str, device: str | None) -> str:
        raise RuntimeError("store unavailable")

    service = VoiceTurnService(
        audio_bridge=AudioBridge(
            codec=Codec(),
            model_sample_rate=16_000,
            queue_capacity=1,
        ),
        model_runtime=runtime,
        recent_context_provider=failing_provider,
    )
    service.accept_opus_uplink(b"one")

    turn = service.process_next_input(target_device_id="living-room")

    assert turn is not None
    assert runtime.contexts == [""]
