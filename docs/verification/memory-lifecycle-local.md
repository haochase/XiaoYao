# Memory Lifecycle Local Check

This record covers the opt-in local memory lifecycle only. It contains no
credential, device identifier, network address, raw audio, transcript, or
personal memory value.

## Scenario

- Storage: SQLite memory rows and pending proposals
- Retention: 60-day default with a bounded value quota
- Cleanup: opt-in application scheduler invoking the existing expiry purge
- Scope: one single-user subject boundary
- Date: 2026-08-12

## Result

The focused memory, scheduler, settings, API, and gateway checks passed:

`84 passed in 2.84s`

The full gateway and utility regression also passed:

`281 passed in 6.40s`

The checks cover disabled-by-default behavior, explicit confirmation before a
durable write, proposal expiry and subject isolation, list/query/export/delete,
60-day retention, quota validation, bounded address-only context, injected-clock
cleanup, and application startup/shutdown ownership of one cleanup loop.

## Boundary

This verifies local storage and lifecycle policy only. It does not prove model
quality, ESP32 behavior, real Feishu delivery, public deployment, or
MiniCPM-o execution on Ascend.
