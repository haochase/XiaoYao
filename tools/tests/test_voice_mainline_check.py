from pathlib import Path
import json

from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from tools import voice_mainline_check
from tools.voice_mainline_check import (
    VoiceCheckResult,
    build_uplink_packets,
    run_check,
)


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "audio"
    / "companion-greeting-zh-cn.wav"
)


def test_build_uplink_packets_produces_valid_xiaozhi_opus_frames() -> None:
    packets = build_uplink_packets(FIXTURE)
    codec = PyAvOpusCodec()

    assert packets
    assert all(codec.decode_uplink(packet).sample_count == 960 for packet in packets)


def test_voice_check_result_serializes_without_sensitive_fields() -> None:
    result = VoiceCheckResult(turns=2, tts_frames=5, elapsed_ms=12.5)

    assert result.as_dict() == {
        "turns": 2,
        "tts_frames": 5,
        "elapsed_ms": 12.5,
    }


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self._events = iter(
            [
                json.dumps({"type": "hello", "session_id": "ses-check"}),
                json.dumps({"type": "tts", "state": "start"}),
                b"tts-opus",
                json.dumps({"type": "tts", "state": "stop"}),
            ]
        )

    def send(self, payload, **kwargs) -> None:
        self.sent.append(payload)

    def recv(self):
        return next(self._events)

    def close(self) -> None:
        return None


def test_run_check_replays_one_turn_and_reports_tts(monkeypatch) -> None:
    socket = FakeSocket()

    class FakeWebSocket:
        class ABNF:
            OPCODE_BINARY = 2

        @staticmethod
        def create_connection(endpoint, *, timeout, header):
            return socket

    monkeypatch.setattr(voice_mainline_check, "websocket", FakeWebSocket)

    result = run_check(
        endpoint="ws://gateway.example.test/v1/devices/ws",
        device_id="device-check",
        token="secret-token",
        audio_wav=FIXTURE,
        turns=1,
        timeout_seconds=1.0,
    )

    assert result.turns == 1
    assert result.tts_frames == 1
    assert all("secret-token" not in str(item) for item in result.as_dict().values())
