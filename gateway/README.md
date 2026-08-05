# XiaoYao Voice Gateway

XiaoYao is a local-first voice companion gateway for ESP32 devices. It uses a
Xiaozhi-compatible WebSocket protocol, raw Opus audio, a pluggable AI runtime,
and a durable task API for reminders and device interactions.

## What is included

- ESP32 DeviceLink with token-digest authentication and session cleanup;
- native `hello`, `listen`, `abort`, and TTS stream controls;
- 16 kHz PCM to 24 kHz Opus conversion through PyAV/libopus;
- a replaceable `ModelRuntime` interface and deterministic test runtime;
- SQLite-backed tasks with idempotency and explicit state transitions;
- health and readiness endpoints; and
- an opt-in, generated audio fixture for local protocol testing.

This repository provides a tested gateway foundation, not a production-ready
assistant. Hardware firmware, model selection, ASR, TTS, and user-facing
channels remain replaceable integrations.

## Requirements

- Python 3.11 or newer
- PyAV/libopus-compatible environment

## Install and test

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[test]"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python -m pytest tests
```

## Run locally

```powershell
$env:PYTHONPATH='src'
$env:COMPANION_DB_PATH='data/companion.db'
$env:COMPANION_DEVICE_TOKEN_HASHES='{}'
.\.venv\Scripts\python -m uvicorn companion_gateway.api:create_default_app --factory --host 127.0.0.1 --port 8723
```

`GET /health` reports process liveness. `GET /ready` separately reports
database availability.

## Connect a device

The gateway keeps SHA-256 token digests, never plaintext device tokens.
Generate a digest without putting the token in shell history:

```powershell
.\.venv\Scripts\python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Device token: ').encode()).hexdigest())"
```

Configure the result before starting the gateway:

```powershell
$env:COMPANION_DEVICE_TOKEN_HASHES='{"dev-living-room":"<sha256-hex>"}'
```

Devices connect to `ws://127.0.0.1:8723/v1/devices/ws` with:

```text
Authorization: Bearer <device-token>
Protocol-Version: 1
Device-Id: dev-living-room
Client-Id: <firmware-client-id>
```

The first text frame is a `hello` message. Audio is accepted only after a
`listen` start control and is returned as `tts.start`, binary Opus frames, and
`tts.stop`.

## Local audio fixture

The checked-in fixture contains generated, non-user audio for deterministic
tests. To regenerate it on Windows:

```powershell
& ..\scripts\generate-audio-fixture.ps1
```

Set `COMPANION_FAKE_VOICE_FIXTURE_PATH` to the generated WAV file to enable a
deterministic local response. It is intended for protocol testing only.
