from __future__ import annotations

import hashlib
import asyncio

import pytest
from pydantic import ValidationError

from companion_gateway.device.camera import (
    CameraCaptureMetadata,
    CameraUploadError,
    CameraUploadState,
    build_vision_capability_message,
    derive_vision_explain_url,
)
from companion_gateway.device.transport import DeviceTransport, OutboundControl
from companion_gateway.device.models import DeviceHello


def hello(*, camera: bool = False) -> DeviceHello:
    features = {"camera_jpeg": camera}
    if camera:
        features["camera_max_bytes"] = 2_097_152
    return DeviceHello.model_validate(
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": features,
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )


def metadata(payload: bytes = b"\xff\xd8image\xff\xd9") -> CameraCaptureMetadata:
    return CameraCaptureMetadata(
        type="camera",
        state="start",
        session_id="ses-1",
        turn_id="turn-1",
        content_type="image/jpeg",
        declared_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_legacy_hello_keeps_camera_disabled_and_camera_metadata_is_strict() -> None:
    assert hello().features.camera_jpeg is False
    assert hello(camera=True).features.camera_max_bytes == 2_097_152
    with pytest.raises(ValidationError):
        CameraCaptureMetadata.model_validate(
            {"type": "camera", "state": "start", "session_id": "ses-1"}
        )


def test_camera_upload_validates_jpeg_length_hash_and_single_active_capture() -> None:
    payload = b"\xff\xd8jpeg-data\xff\xd9"
    upload = CameraUploadState(max_bytes=2_097_152)
    upload.start(metadata(payload))
    with pytest.raises(CameraUploadError, match="already active"):
        upload.start(metadata(payload))
    upload.accept_chunk(payload)

    assert upload.finish() == payload


@pytest.mark.parametrize("error", ["JPEG", "hash"])
def test_camera_upload_rejects_invalid_payload(error: str) -> None:
    upload = CameraUploadState(max_bytes=2_097_152)
    expected_payload = b"\xff\xd8expected\xff\xd9"
    expected = metadata(expected_payload)
    upload.start(expected)
    if error == "JPEG":
        payload = b"x" * len(expected_payload)
    else:
        payload = b"\xff\xd8wrongxxx\xff\xd9"
    upload.accept_chunk(payload)

    with pytest.raises(CameraUploadError, match=error):
        upload.finish()


def test_camera_upload_rejects_size_overflow_and_cleans_state() -> None:
    upload = CameraUploadState(max_bytes=5)
    payload = b"\xff\xd8x\xff\xd9"
    expected = metadata(payload)
    upload.start(expected)

    with pytest.raises(CameraUploadError, match="exceeds"):
        upload.accept_chunk(payload + b"overflow")
    with pytest.raises(CameraUploadError, match="not active"):
        upload.finish()


def test_device_transport_delivers_camera_capture_control() -> None:
    async def scenario() -> None:
        transport = DeviceTransport()
        transport.register("ses-camera")
        transport.send_control(
            "ses-camera",
            {
                "type": "camera",
                "state": "capture",
                "session_id": "ses-camera",
                "turn_id": "turn-1",
                "format": "jpeg",
                "max_bytes": 2_097_152,
            },
        )

        outbound = await transport.next_outbound("ses-camera")

        assert isinstance(outbound, OutboundControl)
        assert outbound.payload["state"] == "capture"

    asyncio.run(scenario())


def test_vision_capability_message_matches_xiaozhi_mcp_initialize_shape() -> None:
    message = build_vision_capability_message(
        "http://192.0.2.20:8723/v1/vision/explain"
    )

    assert message == {
        "type": "mcp",
        "session_id": "ses-camera",
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "vision": {
                        "url": "http://192.0.2.20:8723/v1/vision/explain",
                    }
                }
            },
        },
    }


def test_derive_vision_explain_url_uses_http_for_public_websocket_url() -> None:
    assert derive_vision_explain_url(
        "ws://192.0.2.20:8723/v1/devices/ws"
    ) == "http://192.0.2.20:8723/v1/vision/explain"
