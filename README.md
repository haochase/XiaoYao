# XiaoYao

ESP32 voice companion gateway with Xiaozhi-compatible audio, pluggable AI
runtimes, and durable reminder workflows.

XiaoYao keeps device audio and task handling in a small local service. It is
designed for experimenting with ESP32 voice devices without binding the project
to one model provider or hardware deployment.

## Repository layout

- [`gateway/`](gateway/README.md): FastAPI gateway, audio bridge, task API, and tests.
- [`tools/`](tools/esp32_probe.py): read-only ESP32 board inspection utility.
- [`deploy/ascend/`](deploy/ascend/README.md): public-safe Ascend deployment readiness runbook.
- [`scripts/`](scripts/): audio-fixture generation and optional ESP-IDF build helpers.
- [`assets/audio/`](assets/audio/): generated, non-user test audio fixture.

## Quick start

```powershell
Set-Location gateway
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python -m pytest tests
```

See [`gateway/README.md`](gateway/README.md) for device authentication,
WebSocket controls, and local server setup.

## Private DWS project synchronization

The optional DWS workflow keeps connector credentials and its resource
allowlist outside this repository. It collects only manifest-listed document,
meeting-note, task, and calendar sources, lets the QwenWork project-memory Skill
produce a validated context artifact, and then pushes the result to the
loopback-only synchronization listener at `127.0.0.1:8731`. The ESP32-facing
service on port `8723` does not expose the synchronization routes.

Run the synchronization CLI from the repository root as a module:

```powershell
python -m tools.dws_project_sync --help
```

See [the gateway DWS runbook](gateway/README.md#private-dws-project-synchronization)
for the sanitized manifest schema, required environment-variable names, exact
`collect` and `push` commands, and the five-minute QwenWork schedule. A failed
or overdue source does not have its freshness renewed; answers that depend on
that source remain unavailable until a successful allowed-source refresh.

See [`docs/verification/mimo-v25-smoke.md`](docs/verification/mimo-v25-smoke.md)
for a public-safe runtime verification record. Hardware-specific acceptance
records and deployment notes remain outside the public repository.

## Scope and safety

This repository intentionally excludes local databases, logs, environment
files, firmware images, vendor source snapshots, and hardware backups. Keep
device tokens in environment variables or a secret manager; do not commit them.

The ESP32 build helpers expect their external dependencies under `.vendor/`.
They are not bundled or published by this repository.

## Waveshare XiaoYao firmware profile

The Waveshare ESP32-S3 Audio Board build uses a XiaoYao profile that retains the
board's camera SDK configuration, enables the custom wake word `ni hao xiao
yao`, shows `你好小瑶`, uses WebSocket only after activation, and advertises AFE
voice-activity events for continuous conversation endpoint detection. The OTA
endpoint is required at build time and is rendered into a temporary local
configuration; it is not stored in the tracked profile.

Use an HTTP or HTTPS OTA endpoint without credentials, query parameters, or
fragments. Run the helper from the repository worktree and pass the endpoint
explicitly:

```powershell
.\scripts\build-xiaozhi-waveshare.ps1 -OtaUrl 'https://example.com/ota'
```

If ESP-IDF is already available outside the managed vendor directory, pass its
repository path explicitly:

```powershell
.\scripts\build-xiaozhi-waveshare.ps1 `
  -OtaUrl 'https://example.com/ota' `
  -IdfRepositoryPath 'E:\path\to\esp-idf-v6.0.2'
```

To use a source snapshot outside the default vendor location, provide
`-XiaozhiSourcePath`. The helper applies the XiaoYao patch idempotently, removes
the temporary profile in `finally`, and prints SHA-256 hashes for the generated
images. It does not flash a device.
