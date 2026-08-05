from __future__ import annotations

import av
import numpy as np

from companion_gateway.audio.bridge import (
    AudioFrameRejected,
    DOWNLINK_SAMPLE_RATE,
    UPLINK_SAMPLE_RATE,
    Pcm16Mono,
)


FRAME_DURATION_MS = 60
UPLINK_FRAME_SAMPLES = UPLINK_SAMPLE_RATE * FRAME_DURATION_MS // 1_000
DOWNLINK_FRAME_SAMPLES = DOWNLINK_SAMPLE_RATE * FRAME_DURATION_MS // 1_000
MAX_OPUS_PACKET_BYTES = 4_096


class PyAvOpusCodec:
    """Stateful raw-Opus codec for the Xiaozhi v1 60 ms packet contract."""

    def __init__(self) -> None:
        self._uplink_decoder = av.CodecContext.create("opus", "r")
        self._uplink_decoder.open()
        self._downlink_encoder = self._create_downlink_encoder()

    @staticmethod
    def _create_downlink_encoder() -> av.CodecContext:
        encoder = av.CodecContext.create("opus", "w")
        encoder.sample_rate = DOWNLINK_SAMPLE_RATE
        encoder.layout = "mono"
        encoder.format = "s16"
        encoder.options = {"application": "voip", "frame_duration": "60"}
        encoder.open()
        if encoder.frame_size != DOWNLINK_FRAME_SAMPLES:
            raise RuntimeError(
                "libopus did not configure the required 24 kHz/60 ms frame size"
            )
        return encoder

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        if not payload:
            raise AudioFrameRejected("opus frame is empty")
        if len(payload) > MAX_OPUS_PACKET_BYTES:
            raise AudioFrameRejected("opus frame is too large")

        try:
            decoded = self._uplink_decoder.decode(av.Packet(bytes(payload)))
        except av.FFmpegError as exc:
            raise AudioFrameRejected("opus frame cannot be decoded") from exc
        if not decoded:
            raise AudioFrameRejected("opus frame produced no PCM")

        resampler = av.AudioResampler(
            format="s16",
            layout="mono",
            rate=UPLINK_SAMPLE_RATE,
        )
        converted: list[av.AudioFrame] = []
        for frame in decoded:
            converted.extend(resampler.resample(frame))
        converted.extend(resampler.resample(None))
        pcm = self._pcm_from_frames(converted, sample_rate=UPLINK_SAMPLE_RATE)
        if pcm.sample_count != UPLINK_FRAME_SAMPLES:
            raise AudioFrameRejected(
                "opus frame did not decode to the required 16 kHz/60 ms PCM"
            )
        return pcm

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        if pcm.sample_rate != DOWNLINK_SAMPLE_RATE:
            raise AudioFrameRejected(
                f"downlink PCM is {pcm.sample_rate} Hz, expected "
                f"{DOWNLINK_SAMPLE_RATE} Hz"
            )
        if pcm.sample_count != DOWNLINK_FRAME_SAMPLES:
            raise AudioFrameRejected(
                "downlink PCM must contain exactly one 24 kHz/60 ms frame"
            )

        samples = np.frombuffer(pcm.payload, dtype="<i2").reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = DOWNLINK_SAMPLE_RATE
        try:
            packets = self._downlink_encoder.encode(frame)
        except av.FFmpegError as exc:
            raise AudioFrameRejected("PCM frame cannot be Opus encoded") from exc
        if len(packets) != 1:
            raise AudioFrameRejected("PCM frame did not produce one Opus packet")

        payload = bytes(packets[0])
        if not payload or len(payload) > MAX_OPUS_PACKET_BYTES:
            raise AudioFrameRejected("encoded Opus packet has an invalid size")
        return payload

    @staticmethod
    def _pcm_from_frames(
        frames: list[av.AudioFrame],
        *,
        sample_rate: int,
    ) -> Pcm16Mono:
        if not frames:
            raise AudioFrameRejected("resampler produced no PCM")
        payload = b"".join(
            bytes(frame.planes[0])[: frame.samples * 2]
            for frame in frames
        )
        return Pcm16Mono(sample_rate=sample_rate, payload=payload)
