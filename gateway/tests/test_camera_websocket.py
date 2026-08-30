from __future__ import annotations

import hashlib
import time
from hashlib import sha256

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


DEVICE_ID = "camera-device"
TOKEN = "camera-token"


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Protocol-Version": "1",
        "Device-Id": DEVICE_ID,
        "Client-Id": "camera-client",
    }


def test_camera_capture_control_and_jpeg_upload_are_session_scoped(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "camera-ws.db",
            device_token_hashes={
                DEVICE_ID: sha256(TOKEN.encode("utf-8")).hexdigest()
            },
            camera_enabled=True,
        )
    )
    payload = b"\xff\xd8camera\xff\xd9"

    with TestClient(app) as client:
        with client.websocket_connect("/v1/devices/ws", headers=headers()) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "features": {
                        "camera_jpeg": True,
                        "camera_max_bytes": 2_097_152,
                    },
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            server_hello = websocket.receive_json()
            app.state.device_transport.send_control(
                server_hello["session_id"],
                {
                    "type": "camera",
                    "state": "capture",
                    "session_id": server_hello["session_id"],
                    "turn_id": "turn-camera-1",
                    "format": "jpeg",
                    "max_bytes": 2_097_152,
                },
            )
            assert websocket.receive_json()["state"] == "capture"
            metadata = {
                "type": "camera",
                "state": "start",
                "session_id": server_hello["session_id"],
                "turn_id": "turn-camera-1",
                "content_type": "image/jpeg",
                "declared_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            websocket.send_json(metadata)
            websocket.send_bytes(payload)
            time.sleep(0.02)

    assert app.state.camera_frames.get(
        session_id=server_hello["session_id"],
        turn_id="turn-camera-1",
    ) == payload


def test_camera_capture_endpoint_rejects_legacy_device(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "camera-legacy.db",
            device_token_hashes={
                DEVICE_ID: sha256(TOKEN.encode("utf-8")).hexdigest()
            },
            camera_enabled=True,
        )
    )

    with TestClient(app) as client:
        response = client.post(f"/v1/devices/{DEVICE_ID}/camera/capture")

    assert response.status_code == 409
