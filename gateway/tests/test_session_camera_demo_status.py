from __future__ import annotations

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


def test_demo_status_reports_camera_and_idle_readiness_without_private_values(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "demo-camera.db",
            camera_enabled=True,
            vision_enabled=True,
            device_conversation_idle_timeout_seconds=15,
        )
    )

    with TestClient(app) as client:
        payload = client.get("/v1/demo/status").json()

    assert payload["camera_enabled"] is True
    assert payload["camera_capable_device_online"] is False
    assert payload["recent_image_count"] == 0
    assert payload["conversation_idle_timeout_seconds"] == 15.0
