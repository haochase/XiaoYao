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

## Bootstrap a Xiaozhi device

For a firmware build that requests its WebSocket settings through OTA, configure
an address reachable from the device and an environment-only token map:

```powershell
$env:COMPANION_PUBLIC_WEBSOCKET_URL = 'ws://<lan-host>:8723/v1/devices/ws'
$env:COMPANION_OTA_DEVICE_TOKENS = '{"<device-id>":"<raw-token>"}'
```

The device sends `POST /v1/ota` with its `Device-Id` header. The gateway returns
the matching WebSocket URL, token, and protocol version with `Cache-Control:
no-store`. DeviceLink authentication hashes the same token in memory; raw
tokens must never be committed or logged. Use the host's LAN address, not
`127.0.0.1`.

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

## Voice runtime selection

The default runtime is `none`, which keeps the gateway available for protocol
and task tests without pretending that a model is configured. Select the local
fixture explicitly for deterministic audio replay:

```powershell
$env:COMPANION_VOICE_RUNTIME = 'fixture'
$env:COMPANION_FAKE_VOICE_FIXTURE_PATH = '..\assets\audio\companion-greeting-zh-cn.wav'
```

To use the official MiniCPM-o Realtime API on an Ascend host, select the
WebSocket adapter:

```powershell
$env:COMPANION_VOICE_RUNTIME = 'realtime'
$env:COMPANION_MINICPM_O_ENDPOINT = 'wss://<ascend-host>:9000/v1/realtime?mode=audio'
$env:COMPANION_MINICPM_O_TIMEOUT_SECONDS = '20'
```

The realtime adapter converts the complete stopped turn to base64 float32 PCM
at 16 kHz, waits for text and audio delta events, converts returned 24 kHz
float32 PCM to device Opus, and closes the session. An optional `http` mode is
also available for a local wrapper service using the PCM16 JSON contract in
`voice/minicpm_o.py`. The gateway never imports CANN or `torch_npu`; the
Ascend service is a separate deployment boundary. A gateway-side wrapper may
add a `response.output.delta` event with `kind=task`; that task is validated
before it can enter the local task state machine.

Audio is buffered between `listen.start` and `listen.stop`, so one turn causes
one model request. A runtime failure returns a retryable `model_unavailable`
device error and discards the pending audio. A validated model task is created
idempotently and enters the `scheduled` state; `TaskExecutor.execute_due` can
then advance it through delivery states using a device delivery callback.
