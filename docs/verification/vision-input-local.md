# Single Image Input Local Check

This record covers the local image attachment boundary only. It contains no
image bytes, credential, device identifier, network address, transcript, or
personal content.

## Scenario

- Input: one explicitly consented JPEG, PNG, or WebP upload
- Binding: one active image per subject and voice-turn identifier
- Storage: gateway-local file plus SQLite metadata
- Limits: 10 MB per upload, 200 MB per subject, seven-day retention
- Runtime: audio-only; no video stream and no multimodal model call
- Date: 2026-08-12

## Result

The focused vision, settings, API, scheduler, and gateway checks passed:

`93 passed in 4.69s`

The full gateway and utility regression also passed:

`299 passed in 6.86s`

The checks cover explicit consent, supported MIME types and file signatures,
upload-size and quota limits, one-turn deduplication, subject-scoped listing
and deletion, metadata-only responses, expiry cleanup of both metadata and
local files, injected-clock behavior, and application startup/shutdown
ownership of one cleanup loop.

## Boundary

This verifies local image intake and retention policy only. It does not prove
image understanding, image-to-task execution, ESP32 behavior, real Feishu
delivery, public deployment, or MiniCPM-o execution on Ascend.
