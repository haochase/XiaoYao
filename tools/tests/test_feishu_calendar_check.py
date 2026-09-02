from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import tools.feishu_calendar_check as calendar_check
from companion_gateway.meeting.models import MeetingEvent


NOW = datetime(2026, 8, 27, 4, tzinfo=UTC)


def sensitive_event() -> MeetingEvent:
    return MeetingEvent(
        fingerprint="f" * 64,
        summary="产品周会",
        description_excerpt="raw-description-secret",
        start_at=NOW + timedelta(hours=1),
        end_at=NOW + timedelta(hours=2),
        location="3A会议室",
        status="confirmed",
        rsvp_status="accept",
        is_all_day=False,
    )


def configured_settings():
    return SimpleNamespace(
        feishu_configured=True,
        feishu_app_id="raw-app-id",
        feishu_app_secret="raw-app-secret",
        feishu_receiver_open_id="raw-owner-open-id",
        feishu_base_url="https://open.feishu.example.test/private-path",
        feishu_timeout_seconds=10,
        feishu_max_retries=2,
        feishu_retry_backoff_seconds=1,
        feishu_owner_user_access_token="raw-user-access-token",
        feishu_owner_refresh_token="raw-user-refresh-token",
        feishu_owner_calendar_id="raw-calendar-id",
        feishu_user_token_state_path=Path.cwd() / "raw-state-path",
        meeting_reminder_lead_seconds=3600,
    )


class FakeCalendarClient:
    instances: list["FakeCalendarClient"] = []

    def __init__(self, **kwargs) -> None:
        self.constructor_kwargs = kwargs
        self.calls: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    def list_upcoming(
        self,
        *,
        owner_open_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MeetingEvent, ...]:
        self.calls.append(
            {
                "owner_open_id": owner_open_id,
                "start_at": start_at,
                "end_at": end_at,
            }
        )
        return (sensitive_event(),)


def test_sanitized_event_uses_an_exact_allowlist() -> None:
    result = calendar_check.sanitized_event(sensitive_event())

    assert result == {
        "summary": "产品周会",
        "start_at": "2026-08-27T05:00:00+00:00",
        "end_at": "2026-08-27T06:00:00+00:00",
        "location": "3A会议室",
        "status": "confirmed",
        "rsvp_status": "accept",
        "is_all_day": False,
    }
    assert set(result) == {
        "summary",
        "start_at",
        "end_at",
        "location",
        "status",
        "rsvp_status",
        "is_all_day",
    }


def test_run_check_uses_fake_settings_and_client_without_leaking_inputs() -> None:
    FakeCalendarClient.instances.clear()

    result = calendar_check.run_check(
        settings=configured_settings(),
        hours=24,
        client_factory=FakeCalendarClient,
        clock=lambda: NOW,
    )

    assert set(result) == {"configured", "event_count", "events"}
    assert result["configured"] is True
    assert result["event_count"] == 1
    assert result["events"] == [calendar_check.sanitized_event(sensitive_event())]
    client = FakeCalendarClient.instances[0]
    assert client.constructor_kwargs["owner_user_access_token"] == (
        "raw-user-access-token"
    )
    assert client.constructor_kwargs["owner_refresh_token"] == (
        "raw-user-refresh-token"
    )
    assert client.constructor_kwargs["owner_calendar_id"] == "raw-calendar-id"
    assert client.constructor_kwargs["user_token_state_path"] == (
        Path.cwd() / "raw-state-path"
    )
    assert client.calls == [
        {
            "owner_open_id": "raw-owner-open-id",
            "start_at": NOW,
            "end_at": NOW + timedelta(hours=24),
        }
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in (
        "raw-app-id",
        "raw-app-secret",
        "raw-owner-open-id",
        "raw-user-access-token",
        "raw-user-refresh-token",
        "raw-calendar-id",
        "raw-state-path",
        "raw-description-secret",
        "private-path",
        "f" * 64,
    ):
        assert forbidden not in serialized


def test_run_check_resolves_relative_token_state_from_gateway_directory() -> None:
    FakeCalendarClient.instances.clear()
    settings = configured_settings()
    settings.feishu_user_token_state_path = Path("data/feishu-user-token.json")

    calendar_check.run_check(
        settings=settings,
        hours=24,
        client_factory=FakeCalendarClient,
        clock=lambda: NOW,
    )

    assert FakeCalendarClient.instances[0].constructor_kwargs[
        "user_token_state_path"
    ] == calendar_check.GATEWAY_ENV_PATH.parent / "data/feishu-user-token.json"


def test_cli_loads_only_gateway_env_and_prints_sanitized_json(
    monkeypatch,
    capsys,
) -> None:
    loaded_paths = []
    monkeypatch.setattr(
        calendar_check,
        "load_environment_file",
        lambda path: loaded_paths.append(path) or set(),
    )
    monkeypatch.setattr(
        calendar_check.Settings,
        "from_environment",
        configured_settings,
    )
    monkeypatch.setattr(calendar_check, "FeishuCalendarClient", FakeCalendarClient)
    monkeypatch.setattr(calendar_check, "utc_now", lambda: NOW)

    assert calendar_check.main(["--hours", "12"]) == 0

    captured = capsys.readouterr()
    assert loaded_paths == [calendar_check.GATEWAY_ENV_PATH]
    assert calendar_check.GATEWAY_ENV_PATH.name == ".env"
    assert calendar_check.GATEWAY_ENV_PATH.parent.name == "gateway"
    payload = json.loads(captured.out)
    assert set(payload) == {"configured", "event_count", "events"}
    assert payload["event_count"] == 1
    assert captured.err == ""
    for forbidden in (
        "raw-app-id",
        "raw-app-secret",
        "raw-owner-open-id",
        "raw-description-secret",
        "private-path",
    ):
        assert forbidden not in captured.out


def test_cli_missing_configuration_is_sanitized_and_skips_client(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(calendar_check, "load_environment_file", lambda _path: set())
    monkeypatch.setattr(
        calendar_check.Settings,
        "from_environment",
        lambda: SimpleNamespace(feishu_configured=False),
    )

    def unexpected_client(**_kwargs):
        raise AssertionError("unconfigured diagnostic constructed a client")

    monkeypatch.setattr(calendar_check, "FeishuCalendarClient", unexpected_client)

    assert calendar_check.main([]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "configured": False,
        "event_count": 0,
        "events": [],
    }
    assert captured.err == ""


def test_cli_rejects_invalid_hours_without_printing_the_value(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(calendar_check, "load_environment_file", lambda _path: set())

    assert calendar_check.main(["--hours", "999"]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "configured": False,
        "event_count": 0,
        "events": [],
    }
    assert "999" not in captured.out
    assert captured.err == ""


def test_run_check_dry_run_outputs_only_due_meeting_reminders() -> None:
    FakeCalendarClient.instances.clear()

    result = calendar_check.run_check(
        settings=configured_settings(),
        hours=24,
        client_factory=FakeCalendarClient,
        clock=lambda: NOW,
        dry_run=True,
    )

    assert result["mode"] == "dry_run"
    assert result["device"] == "offline"
    assert result["event_count"] == 1
    assert result["reminder_count"] == 1
    assert result["reminders"] == [
        {
            "summary": "\u4ea7\u54c1\u5468\u4f1a",
            "start_at": "2026-08-27T05:00:00+00:00",
            "end_at": "2026-08-27T06:00:00+00:00",
            "location": "3A\u4f1a\u8bae\u5ba4",
            "text": "\u63d0\u9192\u4f60\uff0c60\u5206\u949f\u540e\u53c2\u52a0\u4ea7\u54c1\u5468\u4f1a\uff0c\u5730\u70b9\u662f3A\u4f1a\u8bae\u5ba4\uff0c\u8bf7\u63d0\u524d\u51c6\u5907\u3002",
        }
    ]


def test_cli_dry_run_is_explicit_and_does_not_print_credentials(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(calendar_check, "load_environment_file", lambda _path: set())
    monkeypatch.setattr(calendar_check.Settings, "from_environment", configured_settings)
    monkeypatch.setattr(calendar_check, "FeishuCalendarClient", FakeCalendarClient)
    monkeypatch.setattr(calendar_check, "utc_now", lambda: NOW)

    assert calendar_check.main(["--hours", "24", "--dry-run"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["device"] == "offline"
    assert payload["reminder_count"] == 1
    assert "raw-app-secret" not in json.dumps(payload, ensure_ascii=False)
