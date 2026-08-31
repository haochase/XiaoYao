from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen as default_urlopen


UrlOpen = Callable[..., Any]
_DEMO_FIELDS = {
    "model_configured",
    "model_canary_ok",
    "tts_configured",
    "tts_canary_ok",
    "feishu_available",
    "device_online",
    "dynamic_agents_enabled",
    "dynamic_agent_count",
    "recent_context_enabled",
    "recent_context_count",
    "camera_enabled",
    "camera_capable_device_online",
    "recent_image_count",
    "conversation_idle_timeout_seconds",
}


def _get_json(base_url: str, path: str, *, timeout_seconds: float, urlopen: UrlOpen):
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return status, payload


def check_gateway(
    base_url: str,
    *,
    timeout_seconds: float = 5.0,
    urlopen: UrlOpen = default_urlopen,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    health_status, health = _get_json(
        base_url, "/health", timeout_seconds=timeout_seconds, urlopen=urlopen
    )
    ready_status, ready = _get_json(
        base_url, "/ready", timeout_seconds=timeout_seconds, urlopen=urlopen
    )
    demo_status, demo = _get_json(
        base_url,
        "/v1/demo/status",
        timeout_seconds=timeout_seconds,
        urlopen=urlopen,
    )
    if set(demo) != _DEMO_FIELDS:
        raise ValueError("/v1/demo/status returned unexpected fields")
    bool_fields = _DEMO_FIELDS - {"dynamic_agent_count", "recent_context_count", "recent_image_count", "conversation_idle_timeout_seconds"}
    if not all(isinstance(demo[field], bool) for field in bool_fields):
        raise ValueError("/v1/demo/status returned invalid boolean fields")
    if not all(isinstance(demo[field], int) for field in {"dynamic_agent_count", "recent_context_count", "recent_image_count"}):
        raise ValueError("/v1/demo/status returned invalid count fields")
    if not isinstance(demo["conversation_idle_timeout_seconds"], (int, float)):
        raise ValueError("/v1/demo/status returned invalid idle timeout")
    healthy = (
        health_status == 200
        and health.get("status") == "ok"
        and ready_status == 200
        and ready.get("status") == "ready"
        and demo_status == 200
        and demo["model_configured"] is True
        and demo["model_canary_ok"] is True
        and demo["tts_configured"] is True
        and demo["tts_canary_ok"] is True
        and demo["feishu_available"] is True
        and demo["conversation_idle_timeout_seconds"] > 0
    )
    return {
        "status": "ok" if healthy else "error",
        "checks": {field: demo[field] for field in sorted(_DEMO_FIELDS)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only XiaoYao session/camera preflight.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8723")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    args = parser.parse_args()
    try:
        result = check_gateway(args.base_url, timeout_seconds=args.timeout_seconds)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        result = {"status": "error", "error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
