from __future__ import annotations

from dataclasses import dataclass

from companion_gateway.audio.bridge import AudioBridge, AudioMetrics, Pcm16Mono
from companion_gateway.voice.runtime import ModelRuntime


@dataclass(frozen=True)
class VoiceTurn:
    input_metrics: AudioMetrics
    response_text: str
    response_metrics: AudioMetrics
    device_opus_frames: tuple[bytes, ...]

    @property
    def device_opus_frame(self) -> bytes:
        if len(self.device_opus_frames) != 1:
            raise ValueError("voice turn contains more than one Opus frame")
        return self.device_opus_frames[0]


class VoiceTurnService:
    def __init__(
        self,
        *,
        audio_bridge: AudioBridge,
        model_runtime: ModelRuntime,
    ) -> None:
        self._audio_bridge = audio_bridge
        self._model_runtime = model_runtime

    def process_next_input(self) -> VoiceTurn | None:
        input_pcm = self._audio_bridge.pop_uplink()
        if input_pcm is None:
            return None

        response = self._model_runtime.respond(input_pcm)
        device_opus_frames = tuple(
            self._audio_bridge.encode_downlink(frame)
            for frame in self._split_response_pcm(response.pcm)
        )
        return VoiceTurn(
            input_metrics=input_pcm.metrics,
            response_text=response.text,
            response_metrics=response.pcm.metrics,
            device_opus_frames=device_opus_frames,
        )

    def accept_opus_uplink(self, payload: bytes) -> None:
        self._audio_bridge.decode_uplink(payload)

    @staticmethod
    def _split_response_pcm(pcm: Pcm16Mono) -> tuple[Pcm16Mono, ...]:
        frame_samples = pcm.sample_rate * 60 // 1_000
        if frame_samples < 1:
            raise ValueError("model PCM sample rate cannot represent a 60 ms frame")
        frame_bytes = frame_samples * 2
        frames: list[Pcm16Mono] = []
        for offset in range(0, len(pcm.payload), frame_bytes):
            payload = pcm.payload[offset : offset + frame_bytes]
            if len(payload) < frame_bytes:
                payload += b"\x00" * (frame_bytes - len(payload))
            frames.append(Pcm16Mono(sample_rate=pcm.sample_rate, payload=payload))
        return tuple(frames)
