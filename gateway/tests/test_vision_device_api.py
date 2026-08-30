from __future__ import annotations

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


class Vision:
    def describe(self, *, image: bytes, prompt: str) -> str:
        assert image.startswith(b"\xff\xd8\xff")
        assert prompt == "看看前面是什么"
        return "前方有一张测试图片"


def test_describe_device_frame_returns_vision_text_and_consumes_frame(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "vision-api.db",
            vision_enabled=True,
            camera_enabled=True,
        ),
        vision_runtime=Vision(),
    )
    app.state.camera_frames.put(
        session_id="ses-vision",
        turn_id="turn-vision",
        payload=b"\xff\xd8\xffjpeg\xff\xd9",
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/sessions/ses-vision/describe",
            json={"turn_id": "turn-vision", "prompt": "看看前面是什么"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "前方有一张测试图片"
    assert app.state.camera_frames.get(
        session_id="ses-vision",
        turn_id="turn-vision",
    ) is None


def test_describe_device_frame_requires_vision_runtime(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "vision-disabled.db",
            vision_enabled=True,
            camera_enabled=True,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/sessions/ses-missing/describe",
            json={"turn_id": "turn-missing", "prompt": "识别"},
        )

    assert response.status_code == 503
