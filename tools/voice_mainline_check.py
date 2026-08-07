from __future__ import annotations

import argparse
import json
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import websocket
except ImportError:  # pragma: no cover - depends on runtime installation
    websocket = None

from companion_gateway.audio.bridge import (
    DOWNLINK_SAMPLE_RATE,
    Pcm16Mono,
    resample_pcm16_mono,
)
from companion_gateway.audio.pyav_opus import (
    DOWNLINK_FRAME_SAMPLES,
    PyAvOpusCodec,
)


DEFAULT_AUDIO_WAV = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "audio"
    / "companion-greeting-zh-cn.wav"
)


@dataclass(frozen=True)
class VoiceCheckResult:
    turns: int
    tts_frames: int
    elapsed_ms: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "turns": self.turns,
            "tts_frames": self.tts_frames,
            "elapsed_ms": self.elapsed_ms,
        }


def _load_wav(path: Path) -> Pcm16Mono:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("audio WAV must be 16-bit mono PCM")
        payload = source.readframes(source.getnframes())
        if not payload:
            raise ValueError("audio WAV must contain samples")
        return Pcm16Mono(sample_rate=source.getframerate(), payload=payload)


def build_uplink_packets(wav_path: Path) -> tuple[bytes, ...]:
    pcm = resample_pcm16_mono(
        _load_wav(wav_path),
        target_sample_rate=DOWNLINK_SAMPLE_RATE,
    )
    frame_bytes = DOWNLINK_FRAME_SAMPLES * 2
    codec = PyAvOpusCodec()
    packets: list[bytes] = []
    for offset in range(0, len(pcm.payload), frame_bytes):
        payload = pcm.payload[offset : offset + frame_bytes]
        payload += b"\x00" * (frame_bytes - len(payload))
        packets.append(
            codec.encode_downlink(
                Pcm16Mono(
                    sample_rate=DOWNLINK_SAMPLE_RATE,
                    payload=payload,
                )
            )
        )
    return tuple(packets)


def _send_json(socket, payload: dict[str, object]) -> None:
    socket.send(json.dumps(payload, separators=(",", ":")))


def _receive_json(socket) -> dict[str, object]:
    raw = socket.recv()
    if isinstance(raw, bytes):
        raise RuntimeError("unexpected binary frame while waiting for JSON")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("gateway JSON frame must be an object")
    return payload


def _receive_tts(socket) -> int:
    while True:
        payload = _receive_json(socket)
        if payload.get("type") == "error":
            raise RuntimeError(f"gateway error: {payload.get('code', 'unknown')}")
        if payload.get("type") == "tts" and payload.get("state") == "start":
            break

    frame_count = 0
    while True:
        raw = socket.recv()
        if isinstance(raw, bytes):
            frame_count += 1
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("type") == "error":
            raise RuntimeError(f"gateway error: {payload.get('code', 'unknown')}")
        if (
            isinstance(payload, dict)
            and payload.get("type") == "tts"
            and payload.get("state") == "stop"
        ):
            return frame_count


def run_check(
    *,
    endpoint: str,
    device_id: str,
    token: str,
    audio_wav: Path,
    turns: int,
    timeout_seconds: float,
) -> VoiceCheckResult:
    if turns < 1:
        raise ValueError("turns must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    if websocket is None:
        raise RuntimeError("websocket-client is required for the acceptance check")

    packets = build_uplink_packets(audio_wav)
    headers = [
        f"Authorization: Bearer {token}",
        "Protocol-Version: 1",
        f"Device-Id: {device_id}",
        "Client-Id: voice-mainline-check",
    ]
    started = time.perf_counter()
    socket = websocket.create_connection(
        endpoint,
        timeout=timeout_seconds,
        header=headers,
    )
    try:
        _send_json(
            socket,
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            },
        )
        hello = _receive_json(socket)
        if hello.get("type") != "hello":
            raise RuntimeError("gateway did not return server hello")
        session_id = hello.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("gateway hello did not contain a session id")

        tts_frames = 0
        for _ in range(turns):
            _send_json(
                socket,
                {"type": "listen", "state": "start", "session_id": session_id},
            )
            for packet in packets:
                socket.send(packet, opcode=websocket.ABNF.OPCODE_BINARY)
            _send_json(
                socket,
                {"type": "listen", "state": "stop", "session_id": session_id},
            )
            tts_frames += _receive_tts(socket)
    finally:
        socket.close()

    elapsed_ms = (time.perf_counter() - started) * 1_000
    return VoiceCheckResult(
        turns=turns,
        tts_frames=tts_frames,
        elapsed_ms=round(elapsed_ms, 2),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the XiaoYao voice mainline")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("COMPANION_DEVICE_ENDPOINT"),
        required="COMPANION_DEVICE_ENDPOINT" not in os.environ,
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("COMPANION_DEVICE_ID"),
        required="COMPANION_DEVICE_ID" not in os.environ,
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("COMPANION_DEVICE_TOKEN"),
        required="COMPANION_DEVICE_TOKEN" not in os.environ,
    )
    parser.add_argument("--audio-wav", type=Path, default=DEFAULT_AUDIO_WAV)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run_check(
            endpoint=args.endpoint,
            device_id=args.device_id,
            token=args.token,
            audio_wav=args.audio_wav,
            turns=args.turns,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
