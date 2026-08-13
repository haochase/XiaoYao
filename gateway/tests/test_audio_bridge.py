from __future__ import annotations

import struct

import pytest

from companion_gateway.audio.bridge import (
    AudioBridge,
    AudioFrameRejected,
    AudioQueueFull,
    Pcm16Mono,
)


def pcm_ramp(*, sample_rate: int, sample_count: int) -> Pcm16Mono:
    samples = [index - (sample_count // 2) for index in range(sample_count)]
    return Pcm16Mono(
        sample_rate=sample_rate,
        payload=struct.pack(f"<{sample_count}h", *samples),
    )


class StubOpusCodec:
    def __init__(self, decoded: Pcm16Mono) -> None:
        self.decoded = decoded
        self.encoded: list[Pcm16Mono] = []

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        if payload == b"truncated":
            raise AudioFrameRejected("opus frame is truncated")
        return self.decoded

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        self.encoded.append(pcm)
        return b"opus:" + pcm.payload[:3]


def test_uplink_16khz_60ms_resamples_to_model_rate() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=24_000, queue_capacity=2)

    result = bridge.decode_uplink(b"valid-opus-frame")

    assert result.sample_rate == 24_000
    assert result.sample_count == 1_440
    assert result.duration_ms == pytest.approx(60.0)
    assert result.metrics.duration_error_ms == pytest.approx(0.0)
    assert result.metrics.peak_abs > 0
    assert result.metrics.non_silent_ratio > 0.99


def test_pcm_metrics_include_root_mean_square_amplitude() -> None:
    pcm = Pcm16Mono(
        sample_rate=16_000,
        payload=struct.pack("<4h", -3, -1, 1, 3),
    )

    assert pcm.metrics.rms_amplitude == pytest.approx((5.0) ** 0.5)


def test_model_pcm_is_resampled_to_24khz_before_opus_encoding() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=2)

    encoded = bridge.encode_downlink(
        pcm_ramp(sample_rate=16_000, sample_count=960)
    )

    assert encoded.startswith(b"opus:")
    assert codec.encoded[0].sample_rate == 24_000
    assert codec.encoded[0].sample_count == 1_440
    assert codec.encoded[0].duration_ms == pytest.approx(60.0)


def test_model_input_and_response_rates_can_differ() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(
        codec=codec,
        model_sample_rate=16_000,
        response_sample_rate=24_000,
        queue_capacity=2,
    )

    encoded = bridge.encode_downlink(
        pcm_ramp(sample_rate=24_000, sample_count=1_440)
    )

    assert encoded.startswith(b"opus:")
    assert codec.encoded[0].sample_rate == 24_000
    assert codec.encoded[0].sample_count == 1_440


@pytest.mark.parametrize("payload", [b"", b"truncated"])
def test_invalid_uplink_frames_are_rejected(payload: bytes) -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)

    with pytest.raises(AudioFrameRejected):
        bridge.decode_uplink(payload)


def test_oversized_uplink_frame_is_rejected() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)

    with pytest.raises(AudioFrameRejected):
        bridge.decode_uplink(b"x" * 4_097)


def test_audio_queue_has_an_explicit_capacity() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)

    bridge.decode_uplink(b"frame-one")

    with pytest.raises(AudioQueueFull):
        bridge.decode_uplink(b"frame-two")


def test_consuming_uplink_audio_releases_queue_capacity() -> None:
    codec = StubOpusCodec(pcm_ramp(sample_rate=16_000, sample_count=960))
    bridge = AudioBridge(codec=codec, model_sample_rate=16_000, queue_capacity=1)

    first = bridge.decode_uplink(b"frame-one")

    assert bridge.pop_uplink() == first
    assert bridge.decode_uplink(b"frame-two").sample_count == 960
