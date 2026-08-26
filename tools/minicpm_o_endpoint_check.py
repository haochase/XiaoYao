from __future__ import annotations

import argparse
import json
import os
import re
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.voice.minicpm_o import (
    MinicpmOHttpRuntime,
    MinicpmORealtimeRuntime,
)
from companion_gateway.voice.runtime import ModelResponse


DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "audio"
    / "companion-greeting-zh-cn.wav"
)


class Runtime(Protocol):
    def respond(self, pcm: Pcm16Mono) -> ModelResponse: ...


RuntimeFactory = Callable[..., Runtime]

AUTH_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_runtime_configuration(*, mode: str, endpoint: str) -> None:
    """Validate the public mode and endpoint contract before creating a runtime."""
    if mode == "http":
        allowed_schemes = {"http", "https"}
        endpoint_kind = "HTTP"
    elif mode == "realtime":
        allowed_schemes = {"ws", "wss"}
        endpoint_kind = "WebSocket"
    else:
        raise ValueError("mode must be http or realtime")

    try:
        parsed = urlparse(endpoint)
        has_userinfo = (
            parsed.username is not None or parsed.password is not None
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("endpoint must be an absolute URL") from exc

    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        raise ValueError(f"endpoint must be an absolute {endpoint_kind} URL")
    if has_userinfo:
        raise ValueError("endpoint must not contain userinfo")
    if mode == "realtime" and parsed.fragment:
        raise ValueError("realtime endpoint must not contain a fragment")


def validate_auth_env_name(auth_env: str) -> None:
    """Reject non-portable environment names before looking them up."""
    if not AUTH_ENV_NAME_PATTERN.fullmatch(auth_env):
        raise ValueError("auth env must be a valid environment variable name")


class SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid command arguments")


def _load_pcm16_mono_16khz_wave(path: Path) -> Pcm16Mono:
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("fixture must be uncompressed PCM WAV")
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("fixture must be 16-bit mono PCM WAV")
        if source.getframerate() != 16_000:
            raise ValueError("fixture must be 16000 Hz PCM WAV")
        return Pcm16Mono(
            sample_rate=16_000,
            payload=source.readframes(source.getnframes()),
        )


def _create_runtime(
    *,
    mode: str,
    endpoint: str,
    auth_token: str | None,
    timeout_seconds: float,
) -> Runtime:
    if mode == "http":
        return MinicpmOHttpRuntime(
            endpoint=endpoint,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
        )
    if mode == "realtime":
        return MinicpmORealtimeRuntime(
            endpoint=endpoint,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError("mode must be http or realtime")


def run_check(
    *,
    mode: str,
    endpoint: str,
    fixture_path: Path,
    turns: int,
    auth_token: str | None,
    runtime_factory: RuntimeFactory = _create_runtime,
    timeout_seconds: float = 20.0,
) -> dict[str, object]:
    """Run one or more MiniCPM-o audio turns and return only safe metrics."""
    validate_runtime_configuration(mode=mode, endpoint=endpoint)
    if turns < 1:
        raise ValueError("turns must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    input_pcm = _load_pcm16_mono_16khz_wave(fixture_path)
    runtime = runtime_factory(
        mode=mode,
        endpoint=endpoint,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )
    results: list[dict[str, int]] = []
    for turn in range(1, turns + 1):
        response = runtime.respond(input_pcm)
        if response.pcm is None:
            raise ValueError("MiniCPM-o response audio is required")
        results.append(
            {
                "turn": turn,
                "duration_ms": round(response.pcm.duration_ms),
                "reply_characters": len(response.text),
                "audio_bytes": len(response.pcm.payload),
                "sample_rate": response.pcm.sample_rate,
            }
        )
    return {"status": "ok", "mode": mode, "turns": turns, "results": results}


def _parser() -> argparse.ArgumentParser:
    parser = SanitizedArgumentParser(
        description="Run a sanitized MiniCPM-o endpoint smoke check."
    )
    parser.add_argument("--mode", choices=("http", "realtime"), default="http")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--auth-env", default="COMPANION_MINICPM_O_AUTH_TOKEN")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        validate_auth_env_name(args.auth_env)
        result = run_check(
            mode=args.mode,
            endpoint=args.endpoint,
            fixture_path=args.fixture,
            turns=args.turns,
            auth_token=os.environ.get(args.auth_env),
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
