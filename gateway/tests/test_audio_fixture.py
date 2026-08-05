from __future__ import annotations

import hashlib
import json
import struct
import wave
from pathlib import Path

import pytest

from companion_gateway.audio.bridge import (
    AudioBridge,
    Pcm16Mono,
    resample_pcm16_mono,
)
from companion_gateway.audio.pyav_opus import PyAvOpusCodec


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIRECTORY = WORKSPACE_ROOT / "assets" / "audio"
WAV_PATH = ASSET_DIRECTORY / "companion-greeting-zh-cn.wav"
MANIFEST_PATH = ASSET_DIRECTORY / "companion-greeting-zh-cn.json"


def frame_peak_abs(frame: bytes) -> int:
    return max(abs(sample) for sample in struct.unpack("<960h", frame))


def test_companion_greeting_fixture_is_16khz_mono_pcm_with_verified_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    with wave.open(str(WAV_PATH), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == 16_000
        duration_seconds = source.getnframes() / source.getframerate()

    assert manifest["text"] == "你好，我在这里。今天想先聊点什么？"
    assert manifest["voice"] == "Microsoft Huihui Desktop"
    assert manifest["sample_rate"] == 16_000
    assert manifest["channels"] == 1
    assert manifest["duration_seconds"] == duration_seconds
    assert manifest["sha256"] == hashlib.sha256(WAV_PATH.read_bytes()).hexdigest()


def test_companion_greeting_60ms_frame_round_trips_through_real_opus() -> None:
    with wave.open(str(WAV_PATH), "rb") as source:
        payload = source.readframes(source.getnframes())

    frame_size = 960
    frames = [
        payload[offset : offset + frame_size * 2]
        for offset in range(0, len(payload) - frame_size * 2 + 1, frame_size * 2)
    ]
    loudest_frame = max(
        frames,
        key=frame_peak_abs,
    )
    source_pcm = Pcm16Mono(sample_rate=16_000, payload=loudest_frame)
    codec = PyAvOpusCodec()
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)

    opus_packet = codec.encode_downlink(
        resample_pcm16_mono(source_pcm, target_sample_rate=24_000)
    )
    replayed_pcm = bridge.decode_uplink(opus_packet)

    assert len(opus_packet) <= 4_096
    assert replayed_pcm.duration_ms == pytest.approx(60.0)
    assert replayed_pcm.metrics.duration_error_ms == pytest.approx(0.0)
    assert replayed_pcm.metrics.peak_abs > 500
    assert replayed_pcm.metrics.non_silent_ratio > 0.9
    assert replayed_pcm.metrics.non_silent_ratio >= (
        source_pcm.metrics.non_silent_ratio - 0.06
    )
