# MiMo-V2.5 Runtime Smoke Check

This record covers the provider adapter only. It contains no API key, raw
audio, transcript, device identifier, network address, or user utterance.

## Scenario

- Runtime: `mimo-v2.5` chat followed by `mimo-v2.5-tts`
- Input: checked-in generated PCM fixture, 16 kHz mono PCM16
- Output: 24 kHz mono PCM16 returned by the TTS adapter
- Turns: 3 sequential requests using the same adapter instance
- Date: 2026-08-12

## Result

| Turn | Result | Latency | Reply characters | TTS bytes | Structured task/action/proposals |
|---:|---|---:|---:|---:|---|
| 1 | passed | 9404.91 ms | 42 | 360960 | none / none / 0 |
| 2 | passed | 5986.46 ms | 49 | 445440 | none / none / 0 |
| 3 | passed | 5622.02 ms | 37 | 337920 | none / none / 0 |

All three turns completed the chat and TTS requests successfully. The adapter
returned valid text and 24 kHz PCM16 audio on every turn. The retry policy was
available but no retry was required during this run.

## Boundary

This check proves the MiMo provider adapter and audio conversion path only. It
does not prove ESP32 wake-word recognition, WebSocket uplink, board playback,
Feishu delivery, or the eventual MiniCPM-o deployment.
