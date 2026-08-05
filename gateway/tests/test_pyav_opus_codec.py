from __future__ import annotations

import math
import struct

import pytest

from companion_gateway.audio.bridge import AudioFrameRejected, Pcm16Mono
from companion_gateway.audio.pyav_opus import PyAvOpusCodec


def pcm_sine(*, sample_rate: int, sample_count: int) -> Pcm16Mono:
    samples = [
        round(12_000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        for index in range(sample_count)
    ]
    return Pcm16Mono(
        sample_rate=sample_rate,
        payload=struct.pack(f"<{sample_count}h", *samples),
    )


def test_real_opus_packet_round_trips_to_16khz_60ms_pcm() -> None:
    codec = PyAvOpusCodec()
    downlink_pcm = pcm_sine(sample_rate=24_000, sample_count=1_440)

    opus_packet = codec.encode_downlink(downlink_pcm)
    uplink_pcm = codec.decode_uplink(opus_packet)

    assert 0 < len(opus_packet) <= 4_096
    assert uplink_pcm.sample_rate == 16_000
    assert uplink_pcm.sample_count == 960
    assert uplink_pcm.duration_ms == pytest.approx(60.0)
    assert uplink_pcm.metrics.non_silent_ratio > 0.95
    assert uplink_pcm.metrics.peak_abs > 1_000


def test_real_opus_codec_rejects_malformed_packet() -> None:
    codec = PyAvOpusCodec()

    with pytest.raises(AudioFrameRejected):
        codec.decode_uplink(b"truncated")
