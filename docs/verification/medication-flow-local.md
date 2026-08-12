# Medication Reminder Local Flow Check

This record covers the deterministic gateway integration path only. It contains
no Feishu credential, access token, device identifier, network address, raw
audio, or user message.

## Scenario

- Runtime: local task service, SQLite repository, fake device delivery, and
  fake Feishu notifier
- Scope: one single-user daily reminder occurrence
- Date: 2026-08-12
- Test command:

```powershell
python -m pytest gateway/tests/test_medication_service.py `
  gateway/tests/test_medication_repository.py `
  gateway/tests/test_medication_scheduler.py `
  gateway/tests/test_feishu_notifier.py `
  gateway/tests/test_device_task_notifications.py `
  gateway/tests/test_memory_api.py `
  gateway/tests/test_api.py -q -o addopts="" -p no:cacheprovider
```

## Result

`58 passed in 3.33s`

The focused suite verifies:

- repeated scheduler ticks create one occurrence and one idempotent task;
- disabled plans do not create new occurrences;
- a due task is adopted as delivered when device delivery succeeds;
- a task that remains undelivered produces the explicit device-offline
  fallback classification;
- a delivered but unacknowledged task produces one timeout fallback after ten
  minutes and never duplicates it;
- a voice acknowledgement is ownership-checked, idempotent, and suppresses
  the fallback;
- Feishu token caching and local 429/5xx retry contracts remain covered; and
- medication and memory API lifecycle responses remain compatible.

## Boundary

This is a local software and provider-contract check. It does not prove ESP32
wake-word recognition, real board playback, real Feishu delivery, a public
gateway deployment, or the eventual MiniCPM-o deployment on Ascend.
