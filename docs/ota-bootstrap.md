# OTA Bootstrap Contract

## Goal

Allow a Xiaozhi-compatible ESP32 firmware to obtain its WebSocket configuration
from a local XiaoYao gateway without embedding credentials in firmware source.

## Configuration

The gateway reads these environment variables at startup:

- `COMPANION_PUBLIC_WEBSOCKET_URL`: the LAN-reachable `ws://` or `wss://` URL
  ending in `/v1/devices/ws`.
- `COMPANION_OTA_DEVICE_TOKENS`: a JSON object that maps each enrolled
  `Device-Id` to its raw device token.

Raw tokens are process configuration. They must not be committed, logged, or
placed in a browser-accessible configuration file. The gateway derives the
SHA-256 digest used by DeviceLink authentication and rejects a conflicting
digest supplied through `COMPANION_DEVICE_TOKEN_HASHES`.

## Endpoint

`POST /v1/ota` requires the standard `Device-Id` request header. The request
body is accepted for firmware compatibility but is not stored or logged.

For an enrolled device, the endpoint returns:

```json
{
  "websocket": {
    "url": "ws://<lan-host>:8723/v1/devices/ws",
    "token": "<device-token>",
    "version": 1
  }
}
```

All responses from this endpoint include `Cache-Control: no-store`. A missing
device identifier is rejected with `400`; an unknown device or disabled
bootstrap configuration is rejected without revealing enrolled identifiers or
tokens.

## Boundaries

- The endpoint is for a trusted LAN only. Bind the server to `0.0.0.0` only on
  the intended network and advertise an explicit LAN address, never
  `127.0.0.1`.
- Firmware obtains its `Device-Id` from its Wi-Fi MAC. That identifier is not
  documented or committed by this repository.
- Each activation response must include the `websocket` object; the firmware
  uses the current response to select its WebSocket protocol.
- This interface does not add a model runtime, task scheduler, or external
  messaging channel.

## Hardware compatibility note

Hardware acceptance is intentionally kept separate from this public contract.
The gateway can verify transport, authentication, the server `hello`, and the
subsequent `listen`/audio exchange, but a successful OTA response alone is not
proof that a board completed a voice round trip. Private acceptance records
must keep serial-port names, LAN addresses, device identifiers, firmware
versions, tokens, and model credentials out of this repository.

## Verification

Automated tests cover configuration validation, enrolled and rejected OTA
requests, no-store responses, and DeviceLink authentication with the token
returned by bootstrap. Hardware acceptance begins only after those tests pass
and the existing flash backup remains available.
