from __future__ import annotations

import json
from datetime import UTC, datetime
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

import companion_gateway.api as api_module
from companion_gateway.api import create_app, create_default_app
from companion_gateway.agent.compiler import AGENT_COMPILER_SYSTEM_PROMPT
from companion_gateway.domain.medication import FeishuSendResult
from companion_gateway.settings import Settings


class CompilerRuntime:
    def respond(self, text: str, *, history=()) -> str:
        return json.dumps(
            {
                "name": "喝水提醒",
                "kind": "reminder",
                "enabled": True,
                "trigger": {"kind": "manual"},
                "channels": ["feishu"],
                "allowed_tools": ["send_feishu"],
                "prompt": "untrusted",
                "memory_policy": "none",
                "max_turns": 1,
                "config": {"message": "请喝水"},
            },
            ensure_ascii=False,
        )


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(
        self,
        *,
        text: str,
        trace_id: str,
        open_id: str | None = None,
    ) -> FeishuSendResult:
        self.messages.append(text)
        return FeishuSendResult(success=True, message_id="om_dynamic_agent")


class RecordingListener:
    is_available = True
    received_messages = 0
    replied_messages = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def dynamic_settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "dynamic-agent-api.db",
        mimo_api_key="example-token",
        dynamic_agents_enabled=True,
        dynamic_agent_owner_id="ou_owner",
        dynamic_agent_target_device_id="living-room",
        dynamic_agent_scheduler_interval_seconds=60,
    )


def test_disabled_dynamic_agents_do_not_expose_management_api(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "disabled.db"))

    with TestClient(app) as client:
        response = client.get("/v1/agents")

    assert response.status_code == 404
    assert app.state.agent_registry is None
    assert app.state.dynamic_agent_scheduler is None


def test_dynamic_agent_api_supports_confirmed_owner_lifecycle(tmp_path) -> None:
    notifier = RecordingNotifier()
    app = create_app(
        dynamic_settings(tmp_path),
        medication_notifier=notifier,
        agent_text_runtime=CompilerRuntime(),
        agent_clock=lambda: datetime(2026, 8, 29, 8, tzinfo=UTC),
    )

    with TestClient(app) as client:
        assert app.state.dynamic_agent_scheduler.is_running is True
        proposed = client.post(
            "/v1/agents/drafts",
            json={
                "request_text": "创建一个提醒喝水的智能体",
                "source_message_id": "om_create_1",
            },
        )
        assert proposed.status_code == 201
        draft = proposed.json()["draft"]

        confirmed = client.post(
            f"/v1/agents/drafts/{draft['draft_id']}/confirm",
        )
        assert confirmed.status_code == 201
        agent = confirmed.json()["agent"]
        agent_id = agent["agent_id"]
        assert "prompt" not in agent
        assert "allowed_tools" not in agent
        assert agent["config"] == {"message": "请喝水"}

        assert client.get("/v1/agents").json()["agents"] == [agent]
        assert client.get(f"/v1/agents/{agent_id}").json()["agent"] == agent

        run = client.post(f"/v1/agents/{agent_id}/run")
        assert run.status_code == 200
        assert run.json()["execution"]["status"] == "succeeded"
        assert notifier.messages == ["请喝水"]

        executions = client.get(f"/v1/agents/{agent_id}/executions")
        assert executions.status_code == 200
        assert executions.json()["executions"][0]["status"] == "succeeded"

        paused = client.post(f"/v1/agents/{agent_id}/pause")
        assert paused.json()["agent"]["enabled"] is False
        blocked = client.post(f"/v1/agents/{agent_id}/run")
        assert blocked.status_code == 409
        resumed = client.post(f"/v1/agents/{agent_id}/resume")
        assert resumed.json()["agent"]["enabled"] is True

        deleted = client.delete(f"/v1/agents/{agent_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/v1/agents/{agent_id}").status_code == 404

    assert app.state.dynamic_agent_scheduler.is_running is False


def test_dynamic_agent_api_rejects_non_local_clients(tmp_path) -> None:
    app = create_app(
        dynamic_settings(tmp_path),
        medication_notifier=RecordingNotifier(),
        agent_text_runtime=CompilerRuntime(),
    )

    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        response = client.get("/v1/agents")

    assert response.status_code == 403


def test_feishu_listener_factory_receives_dynamic_agent_router(tmp_path) -> None:
    captured = {}

    def listener_factory(router):
        captured["router"] = router
        return RecordingListener()

    app = create_app(
        dynamic_settings(tmp_path),
        medication_notifier=RecordingNotifier(),
        agent_text_runtime=CompilerRuntime(),
        feishu_chat_listener_factory=listener_factory,
    )

    assert captured["router"] is app.state.agent_command_router
    assert app.state.feishu_chat_listener is not None


def test_default_app_injects_dynamic_agent_router_into_feishu_listener(
    tmp_path,
    monkeypatch,
) -> None:
    captured = {}
    runtime_prompts = []

    class RecordingTextRuntime:
        def __init__(self, *, system_prompt=None, **kwargs) -> None:
            runtime_prompts.append(system_prompt)

        def respond(self, text: str, *, history=(), agent_context=None) -> str:
            return "{}"

    def fake_create_feishu_chat_listener(*, agent_router=None, **kwargs):
        captured["router"] = agent_router
        return RecordingListener()

    monkeypatch.setattr(
        api_module,
        "LOCAL_ENV_PATH",
        tmp_path / "missing.env",
    )
    monkeypatch.setattr(
        api_module,
        "create_feishu_chat_listener",
        fake_create_feishu_chat_listener,
    )
    monkeypatch.setattr(api_module, "MimoTextChatRuntime", RecordingTextRuntime)
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "default.db"))
    monkeypatch.setenv("COMPANION_MIMO_API_KEY", "example-token")
    monkeypatch.setenv("COMPANION_DYNAMIC_AGENTS_ENABLED", "true")
    monkeypatch.setenv("COMPANION_DYNAMIC_AGENT_OWNER_ID", "ou_owner")
    monkeypatch.setenv(
        "COMPANION_DYNAMIC_AGENT_TARGET_DEVICE_ID",
        "living-room",
    )
    monkeypatch.setenv("COMPANION_FEISHU_CHAT_ENABLED", "true")
    monkeypatch.setenv("COMPANION_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("COMPANION_FEISHU_APP_SECRET", "secret_test")
    monkeypatch.setenv("COMPANION_FEISHU_RECEIVER_OPEN_ID", "ou_owner")

    app = create_default_app()

    assert captured["router"] is app.state.agent_command_router
    assert captured["router"] is not None
    assert len(runtime_prompts) == 2
    assert AGENT_COMPILER_SYSTEM_PROMPT in runtime_prompts
    assert None in runtime_prompts


def test_demo_status_contains_only_sanitized_readiness_fields(tmp_path) -> None:
    app = create_app(
        dynamic_settings(tmp_path),
        medication_notifier=RecordingNotifier(),
        agent_text_runtime=CompilerRuntime(),
    )

    with TestClient(app) as client:
        payload = client.get("/v1/demo/status").json()

    assert payload == {
        "mimo_configured": True,
        "mimo_canary_ok": True,
        "tts_configured": False,
        "tts_canary_ok": False,
        "feishu_available": False,
        "device_online": False,
        "dynamic_agents_enabled": True,
        "dynamic_agent_count": 0,
    }
    serialized = json.dumps(payload)
    assert "living-room" not in serialized
    assert "ou_owner" not in serialized

    with TestClient(app, client=("192.0.2.10", 50000)) as remote_client:
        assert remote_client.get("/v1/demo/status").status_code == 403


def test_dynamic_agent_demo_check_uses_read_only_endpoints() -> None:
    script_path = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "dynamic_agent_demo_check.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dynamic_agent_demo_check",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []

    class Response:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        payloads = {
            "/health": {"status": "ok"},
            "/ready": {
                "status": "ready",
                "checks": {"database": "ok"},
            },
            "/v1/demo/status": {
                "mimo_configured": True,
                "mimo_canary_ok": True,
                "tts_configured": True,
                "tts_canary_ok": True,
                "feishu_available": True,
                "device_online": False,
                "dynamic_agents_enabled": True,
                "dynamic_agent_count": 0,
            },
        }
        return Response(payloads[request.full_url.removeprefix("http://local")])

    result = module.check_gateway(
        "http://local",
        timeout_seconds=1,
        urlopen=fake_urlopen,
    )

    assert result["status"] == "ok"
    assert result["checks"]["mimo_configured"] is True
    assert result["checks"]["mimo_canary_ok"] is True
    assert result["checks"]["device_online"] is False
    assert [request.method for request in requests] == ["GET", "GET", "GET"]
    assert "health" not in result
    assert "ready" not in result
