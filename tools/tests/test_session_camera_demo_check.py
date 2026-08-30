from __future__ import annotations

import json

from tools.session_camera_demo_check import check_gateway


class Response:
    def __init__(self, status: int, payload: dict[str, object]) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_session_camera_check_returns_sanitized_readiness_fields() -> None:
    base_url = "http://127.0.0.1:8723"

    def urlopen(request, *, timeout):
        responses = {
            "/health": Response(200, {"status": "ok"}),
            "/ready": Response(200, {"status": "ready"}),
            "/v1/demo/status": Response(
                200,
                {
                    "mimo_configured": True,
                    "mimo_canary_ok": True,
                    "tts_configured": True,
                    "tts_canary_ok": True,
                    "feishu_available": True,
                    "device_online": True,
                    "dynamic_agents_enabled": True,
                    "dynamic_agent_count": 3,
                    "recent_context_enabled": True,
                    "recent_context_count": 1,
                    "camera_enabled": True,
                    "camera_capable_device_online": True,
                    "recent_image_count": 0,
                    "conversation_idle_timeout_seconds": 15.0,
                },
            ),
        }
        return responses[request.full_url.removeprefix(base_url)]

    result = check_gateway(base_url, timeout_seconds=2, urlopen=urlopen)

    assert result["status"] == "ok"
    assert result["checks"]["camera_capable_device_online"] is True
    assert result["checks"]["conversation_idle_timeout_seconds"] == 15.0
    assert "health" not in result
    assert "ready" not in result
