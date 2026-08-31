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

## Windows startup

After installing the gateway dependencies, review the scheduled-task plan
without registering anything:

```powershell
..\scripts\register-xiaoyao-gateway-task.ps1 -WhatIf
```

Register it only after that review. The task runs the gateway runner when the
current user signs in, listens on port `8723`, and requests up to three restarts
one minute apart after a process failure. The runner changes to this gateway
directory before Uvicorn starts, so the ignored local environment file and the
gateway-relative database path resolve consistently. By default it resolves the
current `python` command; pass an explicit gateway directory, Python executable,
or port when the scheduled task should use a different environment. The selected
Python must have the gateway dependencies installed. Registration verifies that
precondition before creating the task; if it fails, choose an interpreter with
the gateway dependencies and pass it through `-PythonPath`.

```powershell
$python = (Get-Command python -CommandType Application).Source
..\scripts\register-xiaoyao-gateway-task.ps1 -PythonPath $python -Port 8723
..\scripts\check-xiaoyao-gateway-runtime.ps1 -ExpectedHost 0.0.0.0
```

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
`tts.stop`. XiaoYao firmware advertises `features.vad_events=true` and sends
`vad.start` and `vad.stop` controls around speech detected by the ESP32-S3 AFE.

Inspect the current local session without exposing a token or audio payload:

```powershell
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8723/v1/devices/dev-living-room/status
```

The read-only response is `online` for the active session and includes its
phase, listening mode, connection timestamps, and aggregate received-frame
count. An unknown device returns an `offline` snapshot with HTTP 200. Delivery
logs distinguish `device_offline` from `outbound_backpressure`; these reasons
do not change the existing scheduler retry policy.

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
$env:COMPANION_MINICPM_O_AUTH_TOKEN = '<runtime-token>'
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

For the HTTP adapter, configure bounded retries in the ignored `gateway/.env`
file or the process environment:

```text
COMPANION_MINICPM_O_MAX_RETRIES=2
COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS=1
```

Only HTTP 429 and 500-599 responses are retried. The default allows two retries
with 1s then 2s exponential backoff. Authentication errors, other 4xx
responses, transport failures, malformed responses, and realtime failures are
reported immediately. Attempt logs contain only status, attempt number,
duration, or an exception class; request and response data are never logged.

For adapter development without an Ascend account, a deterministic local mock
implements the same HTTP and realtime event contracts:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python -m uvicorn companion_gateway.voice.mock_minicpm_o:app --host 127.0.0.1 --port 9000
```

Use `http://127.0.0.1:9000/v1/infer` for the HTTP adapter or
`ws://127.0.0.1:9000/v1/realtime?mode=audio` for the realtime adapter. The
mock returns generated audio only and must not be used as a production runtime.

The Feishu and dynamic-Agent text channels use the official Chat Completions
request shape with Bearer authentication. Configure their base URL, model, and
bounded retry policy separately from the voice endpoint:

```powershell
$env:COMPANION_MINICPM_O_AUTH_TOKEN = '<your-runtime-token>'
$env:COMPANION_MINICPM_O_COMPATIBLE_BASE_URL = 'http://127.0.0.1:9000/v1'
$env:COMPANION_MINICPM_O_MODEL = 'MiniCPM-O-4.5-9B'
$env:COMPANION_MINICPM_O_COMPATIBLE_MAX_RETRIES = '2'
$env:COMPANION_MINICPM_O_COMPATIBLE_RETRY_BACKOFF_SECONDS = '1'
```

The public non-secret defaults are also listed in `.env.example`. For local
development, put the real token only in the ignored `gateway/.env` file (or in
the current process environment as `COMPANION_MINICPM_O_AUTH_TOKEN`); do not add
it to `.env.example`, source files, logs, test output, commits, or GitHub.

Existing deployments must migrate former provider-specific environment names
to `COMPANION_MINICPM_O_*` before restarting the gateway. Keep
`COMPANION_VOICE_RUNTIME=none` until the new endpoint and token are configured.

Only HTTP 429 and 500-599 responses are retried. The default policy allows two
retries with 1s then 2s exponential backoff. Network failures, authentication
errors, and malformed responses fail immediately as `model_unavailable`; adjust
`COMPANION_MINICPM_O_COMPATIBLE_MAX_RETRIES` and `COMPANION_MINICPM_O_COMPATIBLE_RETRY_BACKOFF_SECONDS` for a
different deployment policy.

For firmware that advertises VAD events, audio outside a complete VAD speech
segment is not added to model input. A valid `vad.stop` ends one turn, and the
same WebSocket remains available for the next `listen.start` and VAD segment.
Short or isolated VAD events are discarded without calling the model or
playing TTS. This is the preferred endpoint source for continuous XiaoYao
conversation.

Set `COMPANION_DEVICE_VAD_TURN_RMS_THRESHOLD` only for VAD-capable firmware.
For initial calibration, inspect `rms_avg` from aggregate
`device_ws_vad_endpoint` logs; after enabling the threshold, use aggregate
`device_ws_vad_rms_rejected` logs to review rejected segments.

Legacy auto-mode firmware can use `COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD`
after inspecting aggregate PCM diagnostics and
`COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES` to end a turn after sustained quiet
audio. The legacy endpoint detector requires
`COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES` consecutive audible frames and
is disabled when its RMS threshold is empty. Auto turns are capped by
`COMPANION_DEVICE_AUTO_TURN_MAX_FRAMES` (150 60 ms frames, about 9 seconds, by
default). VAD firmware uses the independent
`COMPANION_DEVICE_VAD_POST_TTS_RMS_THRESHOLD` (35 by default) to reject speaker
tail audio before accepting the next speech segment.
An unconfirmed turn is discarded without a chat request and receives
no TTS response; the gateway closes that connection normally so auto-listen
firmware cannot feed a retry prompt back into another empty turn.
`COMPANION_AUDIO_QUEUE_CAPACITY` bounds the number of 60 ms uplink frames
retained for one turn; the default of 256 frames supports about 15.36 seconds
of input. A runtime failure returns a retryable
`model_unavailable` device error and discards the pending audio. A validated
model task is created idempotently and enters the `scheduled` state;
`TaskExecutor.execute_due` can then advance it through delivery states using a
device delivery callback.

Voice runtimes may return a structured `VoiceIntent` for current time, current
date, current date and time, or latest reminder status. The gateway ignores the
model-authored reply for those intents, resolves the answer from its
Asia/Shanghai clock or the actor-and-device-scoped task store, and performs TTS
only after grounding. A future MiniCPM-o adapter can reuse this contract; a
runtime that does not emit an intent keeps the normal model response path.
The current voice entry point is intentionally single-user and resolves task
queries as the `voice-user` actor. Speaker identification and per-speaker actor
routing are not implemented.

## Task scheduler

The scheduler is disabled by default. Enable it only after the target device is
reachable and the task notification extension is understood by the firmware:

```powershell
$env:COMPANION_TASK_SCHEDULER_ENABLED = 'true'
$env:COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS = '1'
```

Due tasks are delivered as an additive WebSocket message with
`type=task,state=notify`. Delivery is retried on later ticks while the target
device is offline or its bounded queue is full.

## Medication reminder and Feishu fallback

The first recurring workflow is single-user medication reminder delivery. It
uses `Asia/Shanghai`, accepts one to three daily `HH:MM` times, and defaults to
`08:00`, `12:00`, and `20:00` when the API request omits `reminder_times`.
Create and disable a plan through the local API:

```powershell
$body = @{
  actor_id = 'voice-user'
  target_device_id = 'living-room'
  reminder_times = @('08:00', '12:00', '20:00')
  message = '请确认服药'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8723/v1/medication/plans -Body $body -ContentType 'application/json'
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8723/v1/medication/occurrences
```

The plan sends an existing task notification to the ESP32 at the due time. If
the task remains undelivered for ten minutes, the gateway sends one plain-text
Feishu message stating that the device is offline and voice notification
failed. If the task was delivered but remains unacknowledged for ten minutes,
the message instead states that the voice reminder was sent but not confirmed.
A later scheduler tick cannot send another fallback for that occurrence.

Configure Feishu only in the ignored `gateway/.env` file. The public example
contains names and non-secret defaults, never application credentials:

```text
COMPANION_FEISHU_APP_ID=<enterprise-app-id>
COMPANION_FEISHU_APP_SECRET=<enterprise-app-secret>
COMPANION_FEISHU_RECEIVER_OPEN_ID=<receiver-open-id>
COMPANION_FEISHU_BASE_URL=https://open.feishu.cn
COMPANION_FEISHU_TIMEOUT_SECONDS=10
COMPANION_FEISHU_MAX_RETRIES=2
COMPANION_FEISHU_RETRY_BACKOFF_SECONDS=1
```

The outbound adapter caches `tenant_access_token` in memory and retries only
transport errors, HTTP 429, and HTTP 5xx responses. An optional single-user
private text-chat channel can receive Feishu message events over the official
long connection and route plain text to the configured MiniCPM-o 4.5 model:

```text
COMPANION_FEISHU_CHAT_ENABLED=true
COMPANION_FEISHU_CHAT_HISTORY_TURNS=6
COMPANION_FEISHU_CHAT_STARTUP_TIMEOUT_SECONDS=10
```

The chat channel accepts only the configured receiver `open_id`, rejects group
and non-text messages, drops duplicate message IDs, and keeps only bounded
in-memory conversation context. `帮助` and `清除上下文` are local commands.
It does not expose a public callback, accept arbitrary tools, or call Home
Assistant. The database stays on the gateway host and is never stored on the
ESP32.

## Long-term memory (local API, opt-in)

The first memory slice is disabled by default and stores only explicitly
confirmed profile values in the gateway SQLite database. It does not store raw
audio, transcripts, images, credentials, device tokens, location history, or
health data. When memory is enabled, only the confirmed `address` category is
included in the model prompt, limited to one value and 256 UTF-8 bytes; pending
proposals and all other categories are excluded.

Enable it only for a local test deployment:

```text
COMPANION_MEMORY_ENABLED=true
COMPANION_MEMORY_RETENTION_DAYS=60
COMPANION_MEMORY_QUOTA_BYTES=50000000
COMPANION_MEMORY_PROPOSAL_TTL_SECONDS=600
COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS=86400
```

The local API is subject-scoped and uses the request `X-Trace-Id` as the stored
source identifier:

- `POST /v1/memory/confirm` requires `confirmed: true`.
- `GET /v1/memory?subject_id=<id>&query=<text>&limit=20` lists active values.
- `GET /v1/memory/export?subject_id=<id>` exports active values.
- `DELETE /v1/memory/<memory-id>?subject_id=<id>` removes one value.
- `GET /v1/memory/proposals?subject_id=<id>` lists model suggestions awaiting confirmation.
- `POST /v1/memory/proposals/<proposal-id>/confirm` confirms one suggestion.
- `DELETE /v1/memory/proposals/<proposal-id>?subject_id=<id>` rejects one suggestion.

Expiry cleanup and quota enforcement happen in the gateway process. Keep the
database on the gateway host; at-rest encryption remains a deployment concern
and is not enabled by this local development slice. Only the `address` category
is supplied to the model, limited to one value and 256 UTF-8 bytes; pending
proposals expire after ten minutes.

## Optional single-image input

Vision input is disabled by default. When enabled, a client can upload one
JPEG, PNG, or WebP image for one voice turn to `/v1/vision/observations` with
`X-Subject-Id`, `X-Turn-Id`, and `X-Vision-Consent: true` headers. Images are
stored only under `COMPANION_VISION_STORAGE_PATH`, limited to 10 MB, retained
for seven days, and removed by the opt-in cleanup loop. The upload response
contains metadata and a digest, never image bytes or an absolute path. The
default app does not inject a model vision runtime, so image description stays
unavailable and returns HTTP 503 until a deployment supplies that adapter. The
ESP32 audio WebSocket and model runtime remain audio-only until a separate
multimodal adapter contract is added.

## Narrow agent tools

The local policy layer exposes only two tool routes:

- `POST /v1/agent/tools/query_task_status` reads a task only when the supplied
  actor and target device match the stored task.
- `POST /v1/agent/tools/create_reminder` creates a future reminder in
  `awaiting_confirmation` with `confirmation_policy=required` and an
  idempotency key.

Neither route sends Feishu, writes memory, controls a device, calls an external
URL, or enables automatic execution. The policy is covered by the gateway test
suite.

## Repeatable voice check

Install the gateway dependencies, then configure the device endpoint and token
through the process environment. The token is never included in the JSON result:

```powershell
$env:PYTHONPATH = 'gateway\src'
$env:COMPANION_DEVICE_ENDPOINT = 'ws://<gateway-host>:8723/v1/devices/ws'
$env:COMPANION_DEVICE_ID = '<device-id>'
$env:COMPANION_DEVICE_TOKEN = '<device-token>'
python tools/voice_mainline_check.py --turns 3 --frame-interval 0.06
```

The command replays the checked-in WAV fixture, performs three complete
`hello -> listen.start -> audio -> listen.stop -> tts` turns, and prints JSON
with turn count, returned TTS frame count, and elapsed milliseconds. It does
not access serial ports or change firmware. Audio frames are paced at the
declared 60 ms frame duration by default; pass `--frame-interval 0` only for a
short synthetic test where the gateway queue is configured large enough.
