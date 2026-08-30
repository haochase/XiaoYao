from __future__ import annotations

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.device.models import DeviceHello
from companion_gateway.device.session import DeviceSession
from companion_gateway.settings import Settings


DEVICE_ID = "camera-device"
CLIENT_ID = "camera-client"


class Vision:
    def describe(self, *, image: bytes, prompt: str) -> str:
        assert image.startswith(b"\xff\xd8\xff")
        assert prompt == "what is ahead"
        return "前方有一张测试图片"


def hello() -> DeviceHello:
    return DeviceHello.model_validate(
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"camera_jpeg": True, "camera_max_bytes": 2_097_152},
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )


def multipart(payload: bytes, question: str) -> tuple[bytes, str]:
    boundary = "camera-boundary"
    body = b"".join(
        [
            (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"question\"\r\n\r\n"
            f"{question}\r\n"
            ).encode("utf-8"),
            (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"camera.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
            ).encode("ascii"),
            payload,
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def app_with_session(tmp_path):
    app = create_app(
        Settings(
            database_path=tmp_path / "camera-explain.db",
            camera_enabled=True,
            vision_enabled=True,
            camera_max_bytes=2_097_152,
        ),
        vision_runtime=Vision(),
    )
    session = DeviceSession.create(
        device_id=DEVICE_ID,
        client_id=CLIENT_ID,
        hello=hello(),
    )
    app.state.device_sessions.connect(session)
    return app, session


def test_xiaozhi_multipart_explain_endpoint_returns_vision_result(tmp_path) -> None:
    app, _session = app_with_session(tmp_path)
    body, content_type = multipart(b"\xff\xd8\xffjpeg\xff\xd9", "what is ahead")

    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/explain",
            content=body,
            headers={
                "Content-Type": content_type,
                "Device-Id": DEVICE_ID,
                "Client-Id": CLIENT_ID,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"success": True, "result": "前方有一张测试图片"}


def test_xiaozhi_multipart_explain_rejects_unknown_device(tmp_path) -> None:
    app, _session = app_with_session(tmp_path)
    body, content_type = multipart(b"\xff\xd8\xffjpeg\xff\xd9", "identify")

    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/explain",
            content=body,
            headers={
                "Content-Type": content_type,
                "Device-Id": "unknown-device",
                "Client-Id": CLIENT_ID,
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "message": "camera client unauthorized",
    }


def test_xiaozhi_multipart_explain_returns_safe_failure_without_runtime(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "camera-explain-no-runtime.db",
            camera_enabled=True,
            vision_enabled=True,
        )
    )
    app.state.device_sessions.connect(
        DeviceSession.create(
            device_id=DEVICE_ID,
            client_id=CLIENT_ID,
            hello=hello(),
        )
    )
    body, content_type = multipart(b"\xff\xd8\xffjpeg\xff\xd9", "identify")

    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/explain",
            content=body,
            headers={
                "Content-Type": content_type,
                "Device-Id": DEVICE_ID,
                "Client-Id": CLIENT_ID,
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "message": "vision runtime unavailable",
    }
