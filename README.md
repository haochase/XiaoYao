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

To use a source snapshot outside the default vendor location, provide
`-XiaozhiSourcePath`. The helper applies the XiaoYao patch idempotently, removes
the temporary profile in `finally`, and prints SHA-256 hashes for the generated
images. It does not flash a device.
