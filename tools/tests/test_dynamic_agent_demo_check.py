from __future__ import annotations

import json

from tools.dynamic_agent_demo_check import check_gateway


class FakeResponse:
    def __init__(self, *, status: int, payload: dict[str, object]) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def fake_urlopen(responses: dict[str, FakeResponse]):
    def open_request(request, *, timeout: float):
        assert timeout == 2.0
        return responses[request.full_url]

    return open_request


def test_check_gateway_reports_enabled_agent_count() -> None:
    base_url = "http://127.0.0.1:8723"
    result = check_gateway(
        base_url,
        timeout_seconds=2.0,
        urlopen=fake_urlopen(
            {
                f"{base_url}/health": FakeResponse(
                    status=200,
                    payload={"status": "ok"},
                ),
                f"{base_url}/ready": FakeResponse(
                    status=200,
                    payload={"status": "ready"},
                ),
                f"{base_url}/v1/demo/status": FakeResponse(
                    status=200,
                    payload={
                        "model_configured": True,
                        "model_canary_ok": True,
                        "tts_configured": True,
                        "tts_canary_ok": True,
                        "feishu_available": True,
                        "device_online": True,
                        "dynamic_agents_enabled": True,
                        "dynamic_agent_count": 1,
                    },
                ),
            }
        ),
    )

    assert result["status"] == "ok"
    assert result["checks"]["dynamic_agent_count"] == 1
    assert "health" not in result
    assert "ready" not in result


def test_check_gateway_fails_when_database_is_not_ready() -> None:
    base_url = "http://127.0.0.1:8723"
    result = check_gateway(
        base_url,
        timeout_seconds=2.0,
        urlopen=fake_urlopen(
            {
                f"{base_url}/health": FakeResponse(
                    status=200,
                    payload={"status": "ok"},
                ),
                f"{base_url}/ready": FakeResponse(
                    status=503,
                    payload={"status": "not_ready"},
                ),
                f"{base_url}/v1/demo/status": FakeResponse(
                    status=200,
                    payload={
                        "model_configured": True,
                        "model_canary_ok": False,
                        "tts_configured": True,
                        "tts_canary_ok": False,
                        "feishu_available": False,
                        "device_online": False,
                        "dynamic_agents_enabled": False,
                        "dynamic_agent_count": 0,
                    },
                ),
            }
        ),
    )

    assert result["status"] == "error"
    assert result["checks"]["dynamic_agents_enabled"] is False
    assert result["checks"]["model_canary_ok"] is False
    assert result["checks"]["tts_canary_ok"] is False
