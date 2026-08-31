from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-pixels"


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            database_path=tmp_path / "vision-api.db",
            vision_enabled=True,
            vision_storage_path=tmp_path / "vision-files",
        ),
        vision_clock=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_vision_upload_returns_metadata_without_image_bytes(client: TestClient) -> None:
    response = client.post(
        "/v1/vision/observations",
        headers={
            "Content-Type": "image/png",
            "X-Subject-Id": "family-1",
            "X-Turn-Id": "turn-1",
            "X-Vision-Consent": "true",
        },
        content=PNG,
    )

    assert response.status_code == 201
    body = response.json()["observation"]
    assert body["content_type"] == "image/png"
    assert body["byte_size"] == len(PNG)
    assert "payload" not in body
    assert "storage_path" not in body

    listed = client.get(
        "/v1/vision/observations",
        params={"subject_id": "family-1"},
    )
    assert listed.status_code == 200
    assert listed.json()["observations"][0]["observation_id"] == body[
        "observation_id"
    ]


def test_enabled_vision_scheduler_starts_and_stops_with_app(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "vision-scheduler.db",
            vision_enabled=True,
            vision_storage_path=tmp_path / "vision-files",
        )
    )

    with TestClient(app):
        assert app.state.vision_scheduler.is_running is True

    assert app.state.vision_scheduler.is_running is False


def test_vision_upload_requires_consent_and_rejects_bad_type(client: TestClient) -> None:
    no_consent = client.post(
        "/v1/vision/observations",
        headers={
            "Content-Type": "image/png",
            "X-Subject-Id": "family-1",
            "X-Turn-Id": "turn-1",
        },
        content=PNG,
    )
    bad_type = client.post(
        "/v1/vision/observations",
        headers={
            "Content-Type": "image/gif",
            "X-Subject-Id": "family-1",
            "X-Turn-Id": "turn-2",
            "X-Vision-Consent": "true",
        },
        content=b"GIF89a",
    )

    assert no_consent.status_code == 409
    assert bad_type.status_code == 415


def test_vision_upload_rejects_content_length_over_configured_limit(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "vision-limit.db",
            vision_enabled=True,
            vision_storage_path=tmp_path / "vision-files",
            vision_max_upload_bytes=16,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/observations",
            headers={
                "Content-Type": "image/png",
                "Content-Length": str(len(PNG)),
                "X-Subject-Id": "family-1",
                "X-Turn-Id": "turn-1",
                "X-Vision-Consent": "true",
            },
            content=PNG,
        )

    assert response.status_code == 413


def test_vision_is_disabled_by_default(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "disabled.db"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/vision/observations",
            headers={
                "Content-Type": "image/png",
                "X-Subject-Id": "family-1",
                "X-Turn-Id": "turn-1",
                "X-Vision-Consent": "true",
            },
            content=PNG,
        )

    assert response.status_code == 503
