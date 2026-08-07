from __future__ import annotations

import wave
from pathlib import Path

from companion_gateway.audio.bridge import AudioBridge, Pcm16Mono
from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from companion_gateway.device.transport import DeviceTransport
from companion_gateway.voice.delivery import DeviceVoiceDeliveryService
from companion_gateway.voice.runtime import FakeModelRuntime, ModelRuntime
from companion_gateway.voice.service import VoiceTurnService


FIXTURE_RESPONSE_TEXT = "我在这里，慢慢说。"


def load_pcm16_mono_wave(path: Path) -> Pcm16Mono:
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("voice fixture must be uncompressed PCM WAV")
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("voice fixture must be 16-bit mono PCM WAV")
        return Pcm16Mono(
            sample_rate=source.getframerate(),
            payload=source.readframes(source.getnframes()),
        )


def create_fixture_voice_delivery(
    *,
    fixture_path: Path,
    device_transport: DeviceTransport,
) -> DeviceVoiceDeliveryService:
    response_pcm = load_pcm16_mono_wave(fixture_path)
    return create_voice_delivery(
        model_runtime=FakeModelRuntime(
            response_text=FIXTURE_RESPONSE_TEXT,
            response_pcm=response_pcm,
        ),
        device_transport=device_transport,
        model_sample_rate=response_pcm.sample_rate,
    )


def create_voice_delivery(
    *,
    model_runtime: ModelRuntime,
    device_transport: DeviceTransport,
    model_sample_rate: int = 16_000,
) -> DeviceVoiceDeliveryService:
    bridge = AudioBridge(
        codec=PyAvOpusCodec(),
        model_sample_rate=model_sample_rate,
        queue_capacity=8,
    )
    return DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=bridge,
            model_runtime=model_runtime,
        ),
        device_transport=device_transport,
    )
