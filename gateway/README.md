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

## Private DWS project synchronization

### Protected Runtime Setup

Use a Python interpreter with this worktree's gateway dependencies. The runtime
script explicitly loads the neighboring `gateway/src`; it does not rely on another
worktree's editable installation. Run from the repository root. Replace the two
test-source arguments with your approved private manifest and its exact project key:

```powershell
python tools/dws_sync_runtime.py prepare `
  --manifest 'E:\private\approved-dws-projects.json' `
  --project 'approved-project-key' `
  --dws "$env:USERPROFILE\.qwenworkcn\bin\dws"

python tools/dws_sync_runtime.py check
python tools/dws_sync_runtime.py serve
```

`prepare` requires a real, nonempty validated manifest; it does not discover
sources or select a profile. It refuses an existing configuration/runtime rather
than overwrite credentials. It writes the fixed seven-field task config and a
CurrentUser-DPAPI-encrypted, project/config-bound token below ignored `.private`.
All new output is on E:. C: is permitted only for the existing DWS executable input.
No token is printed or added to the Windows user/system environment.

`serve` is a foreground loopback-only 8731 server with its own private database.
It does not read the device gateway's environment, stop 8723, or wire its database
into the running device service. Device integration is a separate acceptance gate.
Do not proxy this listener or reuse this runtime to grant broader project access.
Stop only the foreground runtime you started after the manual acceptance run.

Import the archive built by `tools/package_dws_context_skill.py` through the
QwenWork Skills page. Local upload is UI-only; files in this repository do not
prove that a skill has been installed. Verify the exact registered name before
running the task. Keep the app's existing storage policy when choosing an install
location; package generation itself never writes to C:.

The production prompt `prompts/qwenwork-dws-project-sync.md` uses
`tools/dws_sync_runtime.py begin/collect/pending/artifact/push/end/abort`.
Only the lease token is passed as a CLI argument. The gateway credential is
decrypted within each wrapper process and supplied only to authenticated gateway
operations; DWS retains the QwenWork session's connector environment.

Version 1 of the context Skill deliberately leaves retrieval completion empty:
a query hash with no question or baseline cannot establish that a request is
resolved. It still creates source-backed context for normal project queries.
`check` does not attest skill registration, login validity or a running server.

### Low-Level Interface

The DWS synchronization listener is a separate, opt-in process. It is fixed to
`127.0.0.1:8731`; it must not be bound to a LAN address, proxied, or mounted on
the ESP32-facing port `8723`. Keep the private manifest, generated source bundle,
QwenWork context artifact, cursor state, and every credential outside Git.

The scheduled task reads only the fixed ignored repository-local configuration
`.private/qwenwork-dws-project-sync.json`. It is a strict schema-version-1 object
with exactly these private value fields: `manifest`, `project`, `dws`,
`source_bundle`, `context_artifact`, and `state`. The committed prompt contains
the field schema but no real values. QwenWork must invoke the fixed
`hui-anchor-dws-project-context-v1` Skill to convert `DwsSourceBundle` into
`QwenProjectContextArtifact`.

The synchronization listener and the QwenWork task must run as the same fixed
Windows user. Evidence is protected with CurrentUser DPAPI, so another user or
a service without that user's loaded profile cannot decrypt it. A protection
identity mismatch or decryption failure closes the affected source instead of
falling back to plaintext.

Configure these names only in an ignored local environment or the process
environment:

```text
COMPANION_PROJECT_API_PRINCIPALS=<principal-to-token-digest-and-project-scope-json>
COMPANION_DWS_SYNC_TOKEN=<matching-raw-bearer-for-the-QwenWork-process>
COMPANION_DEVICE_PROJECT_IDS=<device-to-project-json>
COMPANION_PROJECT_SYNC_HOST=127.0.0.1
COMPANION_PROJECT_SYNC_PORT=8731
COMPANION_PROJECT_SYNC_MAX_BODY_BYTES=2097152
COMPANION_PROJECT_SYNC_CLOCK_SKEW_SECONDS=300
COMPANION_PROJECT_RETRIEVAL_TTL_SECONDS=1800
COMPANION_PROJECT_SOURCE_FRESHNESS_SECONDS=1800
```

`COMPANION_DWS_SYNC_TOKEN` is read directly by `push`; there is no CLI option
for a token or token-variable name. Store only its SHA-256 digest in
`COMPANION_PROJECT_API_PRINCIPALS`. Never place the raw token in the manifest,
command line, generated artifacts, logs, or this repository.

The private manifest is strict JSON. Each project requires a private DWS
`profile`, one permission scope, and at most 30 unique allowlisted sources.
Calendar sources also require a bounded, timezone-aware window. The values below
are placeholders, not real DWS resource identifiers:

```json
{
  "schema_version": 1,
  "projects": [
    {
      "project_id": "project-test-only",
      "project_name": "脱敏测试项目",
      "profile": "<private-dws-profile>",
      "permission_scope": "project:project-test-only",
      "sources": [
        {"source_type": "document", "source_id": "<document-resource-id>"},
        {"source_type": "meeting_note", "source_id": "<meeting-note-resource-id>"},
        {"source_type": "task", "source_id": "<task-resource-id>"},
        {
          "source_type": "calendar",
          "source_id": "<calendar-resource-id>",
          "window_start": "2026-09-01T00:00:00+08:00",
          "window_end": "2026-09-30T23:59:59+08:00"
        }
      ]
    }
  ]
}
```

From the repository root, first check the runner and then start the dedicated
listener. The selected Python must already contain the gateway dependencies:

```powershell
$python = 'E:\python\python.exe'
& $python scripts\run_xiaoyao_sync.py --gateway-root gateway --check
.\scripts\run-xiaoyao-sync.ps1 -GatewayRoot gateway -PythonPath $python
```

In another shell, confirm that 8731 is loopback-only, both health endpoints are
ready, and port 8723 has no synchronization routes:

```powershell
.\scripts\check-xiaoyao-sync-runtime.ps1
```

The following is the low-level manual workflow; the protected runtime setup below
is the production QwenWork entrypoint. Run the manual workflow from the repository root.
Use `python -m tools.dws_project_sync`; invoking the file directly is not the
supported repository import mode.

```powershell
python -m tools.dws_project_sync collect `
  --manifest 'E:\private\dws-projects.json' `
  --project 'project-test-only' `
  --dws-path 'E:\private-tools\dws.exe' `
  --output 'E:\private\dws-source-bundle.json'

python -m tools.dws_project_sync pending `
  --manifest 'E:\private\dws-projects.json' `
  --project 'project-test-only' `
  --sources-file 'E:\private\dws-source-bundle.json' `
  --gateway 'http://127.0.0.1:8731'

# QwenWork now follows prompts/qwenwork-dws-project-sync.md and writes the
# validated QwenProjectContextArtifact to the private context path.

python -m tools.dws_project_sync push `
  --manifest 'E:\private\dws-projects.json' `
  --project 'project-test-only' `
  --sources-file 'E:\private\dws-source-bundle.json' `
  --context-file 'E:\private\qwen-project-context.json' `
  --state-file 'E:\private\dws-sync-state.json' `
  --gateway 'http://127.0.0.1:8731' `
  --dry-run
```

`collect` always supplies the manifest's private profile to the fixed DWS read
commands and requests JSON output. `pending` claims only project-local pending
requests and maps their source hashes back to the manifest whitelist inside the
private source bundle. The QwenWork Skill must cite only active collected sources
and omit unsupported facts. `push --dry-run` validates the
artifact and reports only status, counts, payload size, and a content hash; it
does not call the gateway. Remove `--dry-run` only after explicit approval for
a real synchronization.

These individual module commands remain compatible with approved manual runs
when that project has no active lifecycle lease. A production task must use the
complete `begin -> collect -> pending -> artifact -> push -> end` lifecycle,
pass the same run token to every mutating command, and call `abort` from its
`finally` path on any failure. The project-keyed lease and lock are stored under
the repository's ignored `.private/dws-sync-locks` directory; alternate state
paths cannot bypass them, and concurrent triggers coalesce into at most one
immediate follow-up run.

Create the QwenWork schedule only after one approved manual run. Use the full
contents of `prompts/qwenwork-dws-project-sync.md`, run it every five minutes in
this repository root, keep it disabled until its first manual run succeeds, and
coalesce missed intervals into one recovery run. Any `collect`, `pending`, Skill, or
`push` failure stops that run. Failed sources do not renew freshness, and facts
depending on a source older than 30 minutes remain closed until that source is
successfully refreshed.

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

For pre-production voice testing with MiMo-V2.5, use the OpenAI-compatible
Token Plan endpoint. The gateway sends the stopped 16 kHz PCM turn to
`mimo-v2.5`, then sends the returned reply text to `mimo-v2.5-tts` and receives
24 kHz PCM16 audio for the ESP32:

```powershell
$env:COMPANION_VOICE_RUNTIME = 'mimo'
$env:COMPANION_MIMO_API_KEY = '<your-token-plan-key>'
$env:COMPANION_MIMO_OPENAI_BASE_URL = 'https://token-plan-cn.xiaomimimo.com/v1'
$env:COMPANION_MIMO_ANTHROPIC_BASE_URL = 'https://token-plan-cn.xiaomimimo.com/anthropic'
$env:COMPANION_MIMO_MODEL = 'mimo-v2.5'
$env:COMPANION_MIMO_TTS_MODEL = 'mimo-v2.5-tts'
$env:COMPANION_MIMO_TTS_VOICE = 'mimo_default'
$env:COMPANION_MIMO_MAX_RETRIES = '2'
$env:COMPANION_MIMO_RETRY_BACKOFF_SECONDS = '1'
$env:COMPANION_AUDIO_QUEUE_CAPACITY = '256'
```

The public endpoints and non-secret defaults are also listed in `.env.example`.
For local development, put the real API key only in the ignored `gateway/.env`
file (or in the current process environment as `COMPANION_MIMO_API_KEY`); do
not add it to `.env.example`, source files, logs, test output, commits, or
GitHub. The Anthropic-compatible URL is retained for future tool integrations;
the current audio gateway uses the OpenAI-compatible URL because it supports
the documented audio input and TTS request shapes.

Only HTTP 429 and 500-599 responses are retried. The default policy allows two
retries with 1s then 2s exponential backoff. Network failures, authentication
errors, and malformed responses fail immediately as `model_unavailable`; adjust
`COMPANION_MIMO_MAX_RETRIES` and `COMPANION_MIMO_RETRY_BACKOFF_SECONDS` for a
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
COMPANION_FEISHU_OWNER_USER_ACCESS_TOKEN=<owner-user-access-token>
COMPANION_FEISHU_OWNER_REFRESH_TOKEN=<owner-refresh-token>
COMPANION_FEISHU_OWNER_CALENDAR_ID=<owner-calendar-id>
COMPANION_FEISHU_USER_TOKEN_STATE_PATH=data/feishu-user-token.json
COMPANION_FEISHU_BASE_URL=https://open.feishu.cn
COMPANION_FEISHU_TIMEOUT_SECONDS=10
COMPANION_FEISHU_MAX_RETRIES=2
COMPANION_FEISHU_RETRY_BACKOFF_SECONDS=1
```

The outbound adapter caches `tenant_access_token` in memory and retries only
transport errors, HTTP 429, and HTTP 5xx responses. Optional owner credentials
make calendar reads use the configured calendar directly. When that access
token expires, the OAuth v3 refresh response is written atomically to the
ignored token state path before the rotated token is used; subsequent
processes prefer that state over stale `.env` token values. Separately, an
optional single-user private text-chat channel can receive Feishu message
events over the official long connection and route plain text to the
configured MiMo model:

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

## Feishu meeting assistant (opt-in)

The single-owner meeting assistant is disabled by default. In the Feishu
enterprise app, grant the read-only `calendar:calendar:readonly` permission and
enable the bot capability for the configured owner so the gateway can send
delivery status and fallback text. Publish the app permission change before
testing newly created events.

When owner user credentials are configured, calendar reads use the configured
calendar ID directly and refresh an expired access token through OAuth v3. The
rotated access and refresh tokens are stored atomically at the ignored local
state path. Without owner user credentials, the compatibility path discovers
the readable primary calendar with
`POST /open-apis/calendar/v4/calendars/primarys?user_id_type=open_id`.

Keep credentials only in the ignored `gateway/.env`. Before enabling the
assistant, run the read-only sanitized check from the repository root:

```powershell
$env:PYTHONPATH='gateway\src'
python -B tools\feishu_calendar_check.py --hours 24
```

The command performs no writes. Its JSON contains only `configured`,
`event_count`, and event `summary`, `start_at`, `end_at`, `location`,
`status`, `rsvp_status`, and `is_all_day`. It excludes raw event IDs,
calendar IDs, owner Open IDs, access tokens, descriptions, credentials,
request URLs, absolute private paths, and target device IDs.

When no ESP32 session is available, add --dry-run to print eligible meeting
reminders without calling MiMo, TTS, or the Feishu send API.

~~~powershell
$env:PYTHONPATH='gateway\src'
python -B tools\feishu_calendar_check.py --hours 24 --dry-run
~~~

The dry-run JSON is marked with mode: "dry_run" and device: "offline",
contains only reminders in the configured lead window, and uses the same
deterministic fallback text as online device-offline handling. This is local
diagnostic output, not delivery evidence.

After the sanitized check succeeds, configure the exact P0 settings in
`gateway/.env`:

```text
COMPANION_TASK_SCHEDULER_ENABLED=true
COMPANION_MEETING_ASSISTANT_ENABLED=true
COMPANION_MEETING_TARGET_DEVICE_ID=<device-id>
COMPANION_MEETING_POLL_INTERVAL_SECONDS=30
COMPANION_MEETING_LOOKAHEAD_HOURS=24
COMPANION_MEETING_REMINDER_LEAD_SECONDS=600
COMPANION_MEETING_CONTEXT_TTL_SECONDS=300
```

Enabling requires complete Feishu credentials, a MiMo API key, a target device,
and the task scheduler. A meeting is eligible while
`0 < start_at - now <= 600 seconds`: a meeting exactly 10 minutes away is
included, a late poll can recover it before it starts, and a started or
ineligible event is excluded. The persisted idempotency key prevents repeated
polls or a restart from creating another reminder task. This is task-creation
deduplication; it is not an exactly-once guarantee for external delivery.

MiMo returns one strict preparation label from a fixed allowlist. The gateway
then composes the at-most-80-character briefing deterministically from the
event title, remaining minutes, optional location, and the label's fixed
phrase. Any free-form text, JSON, calendar text, prompt echo, unknown label,
blank output, or model failure uses the deterministic fallback instead. For an
online device, the reminder uses ESP32 TTS and the bot receives a best-effort
delivery-status writeback. If no matching device session exists or TTS fails,
the device-offline fallback sends the same reminder through Feishu.

External delivery is at-least-once across process crash windows. During an
uninterrupted run, a successful attempt is persisted as `DELIVERED` and normal
later ticks do not resend it. A crash after ESP32 TTS or Feishu message creation
but before local `DELIVERED` persistence can replay that side effect on retry;
neither provider has an implemented shared idempotency receipt in this P0.
Grounded `next_meeting` answers use only a fresh, eligible in-memory calendar
snapshot; stale or missing context is reported as unavailable rather than
guessed.

Automated local proof uses fake calendar, model, notifier, and device leaves and
does not prove any external service or hardware:

```powershell
python -B -m pytest gateway\tests tools\tests -p no:cacheprovider
```

Real acceptance uses one owner, one target device, and newly created test
meetings:

1. Run the sanitized check, then create a meeting approximately 12 minutes in
   the future.
2. Start this worktree's gateway on an unused port and confirm one real calendar
   read and one AI briefing.
3. During an uninterrupted run, confirm one ESP32 TTS reminder at the
   10-minute boundary.
4. Ask for the next meeting and confirm title, time, and location match Feishu.
5. Confirm one Feishu delivery-status writeback.
6. Create a second short-term meeting with the device disconnected and, during
   an uninterrupted run, confirm one Feishu fallback and no TTS.

Current real-gate status:

- Real Feishu owner authentication, token rotation, and calendar read:
  `PASS` on 2026-09-02 through two read-only dry-runs; the 24-hour window
  contained zero events, and the second process reused the rotated token state.
- Real eligible meeting reminder candidate: `PASS` on 2026-09-02; one event
  remained visible while the dry-run count changed from zero outside the lead
  window to one inside the 10-minute boundary.
- Real MiMo briefing leaf: `PASS` on 2026-09-02 with an explicitly synthetic
  XiaoYao demo meeting; the validated result used `mode=ai`.
- Real Feishu bot message leaf: `PASS` on 2026-09-02 for the same synthetic
  reminder; the provider returned success and a message ID, and the configured
  recipient confirmed receipt in Feishu.
- Real ESP32 meeting TTS: `PASS` on 2026-09-02. The configured device played
  the full reminder clearly after one negotiated 60 ms frame interval was
  added between `tts.start` and the first Opus frame.
- Real state-driven RGB meeting cue: `PASS` on 2026-09-02. The device ring was
  observed red while listening, briefly green while speaking, and off after
  returning to idle.
- Standalone custom RGB color or blink command: `NOT_IMPLEMENTED`; the current
  xiaozhi 2.4.1 firmware has a six-pixel WS2812 ring but exposes no LED MCP
  tool, so custom control requires a firmware change and flash.
- Real grounded `next_meeting` voice query: `PASS` on 2026-09-02. During the
  full rehearsal, the device answered with the temporary meeting's correct
  name, start time, and location; the user confirmed all three fields.
- Real calendar-to-MiMo-to-Feishu offline fallback: provider-level `PASS` on
  2026-09-02 as one transaction. One temporary event produced one candidate,
  one valid MiMo label, one persisted reminder task, and one delivered fallback;
  the temporary event was deleted and its absence was verified afterward.
- Recipient confirmation for that end-to-end fallback message: `PASS`; the
  configured recipient confirmed receipt in Feishu.
- Real bounded gateway scheduler: `PASS` on 2026-09-02. A temporary local
  configuration started the scheduler, completed two real calendar polls,
  kept a fresh context, stopped cleanly, invoked neither MiMo nor Feishu while
  the calendar was empty, and removed its temporary database after shutdown.
- Persistent local gateway activation: `PASS` on 2026-09-02. The existing
  `XiaoYao Voice Gateway` task was updated to this Feishu worktree, remained
  running across a complete 30-second meeting poll interval, and returned
  healthy and ready responses before and after that interval.
- Real Feishu private text channel: `PASS` on 2026-09-02. The configured owner
  sent a normal chat message, received a MiMo reply, and then completed the
  local `清除上下文` command; gateway counters reached three received and three
  replied messages.
- Real medication reminder: `PASS` on 2026-09-02. A temporary one-time plan
  created one occurrence, delivered one stable ESP32 voice reminder confirmed
  by the user, acknowledged the occurrence, disabled the plan, and left no
  enabled plan or pending Feishu fallback.
- Full competition rehearsal: `PASS` on 2026-09-02 for calendar polling,
  T-10 selection, MiMo briefing, stable ESP32 TTS, state-driven RGB indication,
  and grounded next-meeting voice response. The temporary event was deleted
  after the rehearsal.

Do not treat automated substitutes, fixtures, or old logs as real acceptance.
P0 is not complete until one fresh Feishu to AI to ESP32 to Feishu loop is
observed. Meeting indicator behavior and its settings belong to the separate
P1 scope.

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
URL, or enables automatic execution. See
[`docs/verification/agent-tools-local.md`](../docs/verification/agent-tools-local.md)
for the local policy check.

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
