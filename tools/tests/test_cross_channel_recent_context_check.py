from __future__ import annotations

import json

from tools.cross_channel_recent_context_check import check_gateway


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


def test_recent_context_check_returns_only_safe_fields() -> None:
    base_url = "http://127.0.0.1:8723"

    def urlopen(request, *, timeout):
        paths = {
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
                    "device_online": False,
                    "dynamic_agents_enabled": True,
                    "dynamic_agent_count": 3,
                    "recent_context_enabled": True,
                    "recent_context_count": 1,
                },
            ),
        }
        return paths[request.full_url.removeprefix(base_url)]

    result = check_gateway(base_url, timeout_seconds=2, urlopen=urlopen)

    assert result["status"] == "ok"
    assert result["checks"] == {
        "device_online": False,
        "dynamic_agent_count": 3,
        "dynamic_agents_enabled": True,
        "feishu_available": True,
        "mimo_canary_ok": True,
        "mimo_configured": True,
        "recent_context_count": 1,
        "recent_context_enabled": True,
        "tts_canary_ok": True,
        "tts_configured": True,
    }
