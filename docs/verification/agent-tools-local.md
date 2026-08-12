# Narrow Agent Tools Local Check

This record covers the gateway-owned tool boundary only. It contains no model
credential, device token, network address, Feishu message, or user data.

## Enabled tools

- `query_task_status`: read-only, actor/device scoped, 10-second budget.
- `create_reminder`: creates a future `reminder` task with
  `confirmation_policy=required`; it never schedules delivery or executes a
  device action directly.

The allowlist is closed. Feishu, memory writes, device control, arbitrary
network calls, and automatic execution are not tool operations.

## Result

The focused Agent API and service checks passed:

`8 passed in 1.33s`

The checks cover the closed allowlist, subject/device isolation, required
confirmation, future-time validation, idempotency, nested `auto_execute`
override rejection, and the 10-second timeout contract. The nested override is
ignored because execution policy is gateway-owned. A timeout after a
local idempotent write may return an error to the caller; repeating the same
idempotency key returns the existing awaiting-confirmation proposal and never
creates a duplicate.

## Boundary

This is a local policy-layer check. It does not prove model tool-call parsing,
ESP32 behavior, real Feishu delivery, image reasoning, public deployment, or
MiniCPM-o execution on Ascend.
