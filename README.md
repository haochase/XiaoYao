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

## XiaoQian project memory assistant

XiaoQian (小千) is the project-memory desktop meeting assistant built on this
shared XiaoYao gateway. It uses QwenWork and allowlisted DWS sources to maintain
source-backed project decisions, then lets an ESP32 voice terminal query the
current decision without storing project documents or model credentials on the
device. Stale, unauthorized, or unsupported facts fail closed instead of being
guessed.

The same QwenWork Skill package also defines source-bound pre-meeting points and
review-bound post-meeting reports. Pre-meeting output can only select facts from
validated project memory; post-meeting accepted, rejected, and pending sections
must match the local review snapshot before the workflow records success.

The existing `hui-anchor` branch, project IDs, and
`hui-anchor-dws-project-context-v1` Skill ID remain compatibility identifiers;
XiaoQian is the product display name.

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

Individual `collect`, `pending`, and `push` commands remain available for an
approved manual run when no project lifecycle is active. The current production
QwenWork path validates the Git-tracked official DWS Core approval, then runs
`check`, `check-core`, `begin`, `collect-direct`, `pending`,
`reuse-artifact --unattended`, `push`, and `end`; every post-begin failure calls
`abort`. An approved manual refresh instead generates and validates a new
artifact, performs a dry-run push, then performs the real push. The older host-import
implementation remains compatibility code for one version and is never an
automatic fallback. A Core version, hash, publisher, or signing-certificate
change requires an explicit user-approved update to the Git approval catalog.
The project-keyed lease lives under the repository's ignored
`.private/dws-sync-locks` directory, so alternate state-file paths cannot run
the same project concurrently. Concurrent schedule triggers are coalesced into
one follow-up run.

Scheduled QwenWork runs use the fixed ignored task configuration at
`.private/qwenwork-dws-project-sync.json`. Its schema-version-1 fields are
`manifest`, `project`, `dws`, `source_bundle`, `context_artifact`, and `state`;
real values remain outside Git. The context conversion Skill has the fixed name
`hui-anchor-dws-project-context-v1`.

The five-minute QwenWork task stays paused by default. Neither the unattended
nor manual prompt enables it. Keep the DWS profile, source text, private
configuration, credentials, generated artifacts, and verification records out
of Git.

Its public source lives in `skills/hui-anchor-dws-project-context-v1`.
Package it with `python -m tools.package_dws_context_skill --output E:\path\context.zip`,
then import the ZIP through QwenWork's Skills page and verify its registered name.
Building the archive does not install or enable it. Version 1 does not assert
retrieval completion from a query hash alone.

For protected local setup, use `python tools/dws_sync_runtime.py --help` and
the [runtime setup section](gateway/README.md#protected-runtime-setup).

Conflict acceptance and rejection use the separate fixed-boundary reviewer
client. It reads only `principal.json` and the CurrentUser-DPAPI protected
`credential.dpapi` from `E:\haochase\xiaoqian\.private\project-review`, contacts only
`http://127.0.0.1:8724`, and proceeds only when the fixed project has exactly
one proposed candidate. The client accepts no endpoint, project, candidate,
reviewer, or token override:

```powershell
python tools/project_conflict_review.py list
python tools/project_conflict_review.py accept --reason '负责人确认采用会议候选方案'
python tools/project_conflict_review.py reject --reason '保留现有正式决策'
```

Review output is deliberately redacted: it reports only readiness or terminal
status, never the token, candidate identifier, decision text, or source details.
The gateway derives reviewer identity, decision text, evidence, and review time
from the authenticated principal and stored candidate rather than client input.

See [the gateway DWS runbook](gateway/README.md#private-dws-project-synchronization)
for the sanitized manifest schema, required environment-variable names, the
manual `collect` compatibility command, the production direct-Core flow, and
the paused five-minute QwenWork task. A failed
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
