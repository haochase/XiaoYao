from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_path=tmp_path / "agent-api.db"),
        agent_clock=lambda: NOW,
    )
    with TestClient(app) as test_client:
        yield test_client


def reminder_request() -> dict[str, object]:
    return {
        "actor_id": "family-1",
        "target_device_id": "living-room",
        "arguments": {
            "schedule": {
                "at": "2026-08-12T12:05:00+00:00",
                "timezone": "Asia/Shanghai",
            },
            "text": "请提醒我喝水",
            "idempotency_key": "api-agent-reminder-1",
        },
    }


def test_agent_api_creates_required_proposal_and_queries_status(client: TestClient) -> None:
    response = client.post(
        "/v1/agent/tools/create_reminder",
        headers={"X-Trace-Id": "trace-agent-create"},
        json=reminder_request(),
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["tool"] == "create_reminder"
    assert result["requires_confirmation"] is True
    assert result["auto_executed"] is False

    queried = client.post(
        "/v1/agent/tools/query_task_status",
        json={
            "actor_id": "family-1",
            "target_device_id": "living-room",
            "arguments": {"task_id": result["task_id"]},
        },
    )

    assert queried.status_code == 200
    assert queried.json()["result"]["found"] is True


def test_agent_api_rejects_forbidden_tool_and_extra_fields(client: TestClient) -> None:
    forbidden = client.post(
        "/v1/agent/tools/send_feishu",
        json={
            "actor_id": "family-1",
            "target_device_id": "living-room",
            "arguments": {},
        },
    )
    invalid = client.post(
        "/v1/agent/tools/query_task_status",
        json={
            "actor_id": "family-1",
            "target_device_id": "living-room",
            "arguments": {"task_id": "missing", "auto_execute": True},
            "extra": "rejected",
        },
    )

    assert forbidden.status_code == 404
    assert invalid.status_code == 422


def test_agent_api_ignores_nested_auto_execute_override(client: TestClient) -> None:
    request = reminder_request()
    request["arguments"]["auto_execute"] = True

    response = client.post(
        "/v1/agent/tools/create_reminder",
        json=request,
    )

    assert response.status_code == 200
    assert response.json()["result"]["auto_executed"] is False
    assert response.json()["result"]["requires_confirmation"] is True
