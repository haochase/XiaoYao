# XiaoYao

ESP32 voice companion gateway with Xiaozhi-compatible audio, pluggable AI
runtimes, and durable reminder workflows.

XiaoYao keeps device audio and task handling in a small local service. It is
designed for experimenting with ESP32 voice devices without binding the project
to one model provider or hardware deployment.

## Repository layout

- [`gateway/`](gateway/README.md): FastAPI gateway, audio bridge, task API, and tests.
- [`tools/`](tools/esp32_probe.py): read-only ESP32 board inspection utility.
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

## Scope and safety

This repository intentionally excludes local databases, logs, environment
files, firmware images, vendor source snapshots, and hardware backups. Keep
device tokens in environment variables or a secret manager; do not commit them.

The ESP32 build helpers expect their external dependencies under `.vendor/`.
They are not bundled or published by this repository.
