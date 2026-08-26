from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.voice.runtime import ModelResponse

from tools.minicpm_o_endpoint_check import main, run_check


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "assets" / "audio" / "companion-greeting-zh-cn.wav"
GATEWAY_SRC = ROOT / "gateway" / "src"
SMOKE_TEMP_ROOT = ROOT / ".vendor" / "temp"


@contextmanager
def _running_mock_minicpm_o_server(
    env: dict[str, str],
) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "companion_gateway.voice.mock_minicpm_o:app",
            "--host",
            "127.0.0.1",
            "--port",
            "9000",
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("MiniCPM-o mock server exited before becoming ready")
            try:
                with urlopen(
                    "http://127.0.0.1:9000/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.1)
        else:
            pytest.fail("MiniCPM-o mock server did not become ready")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class FakeRuntime:
    def __init__(self) -> None:
        self.received: list[Pcm16Mono] = []

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received.append(pcm)
        return ModelResponse(
            text="hidden reply",
            pcm=Pcm16Mono(sample_rate=24_000, payload=b"\x01\x00" * 1_440),
        )


def test_run_check_aggregates_sanitized_results() -> None:
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeRuntime:
        calls.append(kwargs)
        return FakeRuntime()

    result = run_check(
        mode="http",
        endpoint="http://127.0.0.1:9000/v1/infer",
        fixture_path=FIXTURE_PATH,
        turns=3,
        auth_token="secret-token",
        runtime_factory=factory,
    )

    assert result["status"] == "ok"
    assert result["mode"] == "http"
    assert result["turns"] == 3
    assert len(result["results"]) == 3
    assert result["results"][0] == {
        "turn": 1,
        "duration_ms": 60,
        "reply_characters": 12,
        "audio_bytes": 2_880,
        "sample_rate": 24_000,
    }
    assert calls == [
        {
            "mode": "http",
            "endpoint": "http://127.0.0.1:9000/v1/infer",
            "auth_token": "secret-token",
            "timeout_seconds": 20.0,
        }
    ]
    serialized = json.dumps(result)
    assert "secret-token" not in serialized
    assert "hidden reply" not in serialized
    assert "audio_base64" not in serialized


@pytest.mark.parametrize(
    ("mode", "endpoint"),
    [
        ("http", "http://127.0.0.1:9000/v1/infer"),
        ("realtime", "ws://127.0.0.1:9000/v1/realtime?mode=audio"),
    ],
)
def test_run_check_selects_each_supported_runtime(mode: str, endpoint: str) -> None:
    selected: list[str] = []

    def factory(**kwargs: object) -> FakeRuntime:
        selected.append(str(kwargs["mode"]))
        return FakeRuntime()

    result = run_check(
        mode=mode,
        endpoint=endpoint,
        fixture_path=FIXTURE_PATH,
        turns=1,
        auth_token=None,
        runtime_factory=factory,
    )

    assert result["status"] == "ok"
    assert selected == [mode]


@pytest.mark.parametrize(
    ("mode", "endpoint"),
    [
        ("unsupported", "http://127.0.0.1:9000/v1/infer"),
        ("http", "ws://127.0.0.1:9000/v1/realtime?mode=audio"),
        ("realtime", "http://127.0.0.1:9000/v1/infer"),
        ("http", "http://user:secret-token@127.0.0.1:9000/v1/infer"),
    ],
)
def test_run_check_rejects_invalid_runtime_configuration_before_factory(
    mode: str, endpoint: str
) -> None:
    factory_called = False

    def factory(**_: object) -> FakeRuntime:
        nonlocal factory_called
        factory_called = True
        return FakeRuntime()

    with pytest.raises(ValueError):
        run_check(
            mode=mode,
            endpoint=endpoint,
            fixture_path=FIXTURE_PATH,
            turns=1,
            auth_token="secret-token",
            runtime_factory=factory,
        )

    assert not factory_called


def test_run_check_rejects_non_pcm16_mono_16khz_wave(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid.wav"
    with wave.open(str(invalid_fixture), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 4)

    with pytest.raises(ValueError, match="16-bit mono PCM WAV"):
        run_check(
            mode="http",
            endpoint="http://127.0.0.1:9000/v1/infer",
            fixture_path=invalid_fixture,
            turns=1,
            auth_token=None,
            runtime_factory=lambda **_: FakeRuntime(),
        )


def test_run_check_rejects_turns_below_one() -> None:
    with pytest.raises(ValueError, match="turns"):
        run_check(
            mode="http",
            endpoint="http://127.0.0.1:9000/v1/infer",
            fixture_path=FIXTURE_PATH,
            turns=0,
            auth_token=None,
            runtime_factory=lambda **_: FakeRuntime(),
        )


def test_run_check_propagates_runtime_failure() -> None:
    class FailingRuntime:
        def respond(self, pcm: Pcm16Mono) -> ModelResponse:
            raise RuntimeError("endpoint and secret-token must stay private")

    with pytest.raises(RuntimeError, match="private"):
        run_check(
            mode="http",
            endpoint="http://127.0.0.1:9000/v1/infer",
            fixture_path=FIXTURE_PATH,
            turns=1,
            auth_token="secret-token",
            runtime_factory=lambda **_: FailingRuntime(),
        )


@pytest.mark.parametrize("mode", ["http", "realtime"])
def test_cli_loads_auth_environment_without_printing_value(
    mode: str, monkeypatch, capsys
) -> None:
    received: list[str | None] = []

    def fake_run_check(**kwargs: object) -> dict[str, object]:
        received.append(kwargs["auth_token"])
        return {"status": "ok", "mode": mode, "turns": 1, "results": []}

    monkeypatch.setattr("tools.minicpm_o_endpoint_check.run_check", fake_run_check)
    monkeypatch.setenv("CHECKER_TOKEN", "secret-token")

    assert main(
        [
            "--mode",
            mode,
            "--endpoint",
            "http://127.0.0.1:9000/v1/infer",
            "--auth-env",
            "CHECKER_TOKEN",
            "--json",
        ]
    ) == 0

    captured = capsys.readouterr()
    assert received == ["secret-token"]
    assert json.loads(captured.out)["status"] == "ok"
    assert "secret-token" not in captured.out
    assert captured.err == ""


def test_cli_returns_sanitized_json_error_and_nonzero_exit(capsys) -> None:
    assert main(
        [
            "--endpoint",
            "http://user:secret-token@127.0.0.1:9000/v1/infer",
            "--turns",
            "0",
            "--json",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "error", "error_type": "ValueError"}
    assert "secret-token" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("auth_env", ["INVALID-NAME", "1INVALID"])
def test_cli_rejects_invalid_auth_environment_name(
    auth_env: str, monkeypatch, capsys
) -> None:
    def fake_run_check(**_: object) -> dict[str, object]:
        return {"status": "ok", "mode": "http", "turns": 1, "results": []}

    monkeypatch.setattr("tools.minicpm_o_endpoint_check.run_check", fake_run_check)

    assert main(
        [
            "--endpoint",
            "http://127.0.0.1:9000/v1/infer",
            "--auth-env",
            auth_env,
            "--json",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "error", "error_type": "ValueError"}
    assert captured.err == ""


def test_cli_subprocess_smoke_exercises_http_and_realtime_runtimes() -> None:
    auth_env = "MINICPM_O_ENDPOINT_SMOKE_TOKEN"
    auth_token = "local-smoke-token-not-for-output"
    raw_http_audio = base64.b64encode(b"\x02\x00" * 1_440).decode("ascii")
    SMOKE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="minicpm-o-endpoint-smoke-", dir=SMOKE_TEMP_ROOT
    ) as temp_dir:
        env = os.environ.copy()
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(GATEWAY_SRC), current_pythonpath) if item
        )
        env["TEMP"] = temp_dir
        env["TMP"] = temp_dir
        env[auth_env] = auth_token
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"

        with _running_mock_minicpm_o_server(env) as server:
            for mode, endpoint in (
                ("http", "http://127.0.0.1:9000/v1/infer"),
                ("realtime", "ws://127.0.0.1:9000/v1/realtime?mode=audio"),
            ):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "tools/minicpm_o_endpoint_check.py",
                        "--mode",
                        mode,
                        "--endpoint",
                        endpoint,
                        "--auth-env",
                        auth_env,
                        "--json",
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )

                assert completed.returncode == 0
                payload = json.loads(completed.stdout)
                assert payload["status"] == "ok"
                assert payload["mode"] == mode
                assert payload["results"][0]["sample_rate"] == 24_000
                assert endpoint not in completed.stdout
                assert auth_token not in completed.stdout
                assert "I am here." not in completed.stdout
                assert "audio_base64" not in completed.stdout
                assert raw_http_audio not in completed.stdout

        assert server.poll() is not None
