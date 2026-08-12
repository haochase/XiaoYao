import json
from urllib.error import HTTPError

import pytest

from companion_gateway.notifications.feishu import FeishuNotifier


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def make_notifier(urlopen):
    return FeishuNotifier(
        app_id="cli_test_app",
        app_secret="secret_test_value",
        receiver_open_id="ou_test_receiver",
        base_url="https://open.feishu.test",
        timeout_seconds=1,
        max_retries=2,
        retry_backoff_seconds=0,
        urlopen=urlopen,
        sleep=lambda _seconds: None,
    )


def test_token_is_cached_and_message_id_is_returned() -> None:
    calls: list[str] = []

    def urlopen(request, *, timeout):
        calls.append(request.full_url)
        if "/tenant_access_token/" in request.full_url:
            return FakeResponse({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_test_message"}})

    notifier = make_notifier(urlopen)

    first = notifier.send_text(text="服药提醒", trace_id="trace-one")
    second = notifier.send_text(text="服药提醒", trace_id="trace-two")

    assert first.success is True
    assert first.message_id == "om_test_message"
    assert second.success is True
    assert calls.count("https://open.feishu.test/open-apis/auth/v3/tenant_access_token/internal") == 1
    assert calls.count("https://open.feishu.test/open-apis/im/v1/messages?receive_id_type=open_id") == 2


def test_429_and_5xx_are_retried_within_budget() -> None:
    attempts = 0

    def urlopen(request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(request.full_url, 429, "rate limited", {}, None)
        if attempts == 2:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        if "/tenant_access_token/" in request.full_url:
            return FakeResponse({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return FakeResponse({"code": 0, "data": {"message_id": "om_retry_message"}})

    result = make_notifier(urlopen).send_text(text="服药提醒", trace_id="trace-retry")

    assert result.success is True
    assert result.message_id == "om_retry_message"
    assert attempts == 4


def test_non_retryable_provider_error_is_redacted() -> None:
    def urlopen(request, *, timeout):
        return FakeResponse({"code": 999, "msg": "bad secret details"})

    result = make_notifier(urlopen).send_text(text="服药提醒", trace_id="trace-error")

    assert result.success is False
    assert result.message_id is None
    assert result.error == "provider_code_999"
    assert "secret" not in result.error
    assert "cli_test_app" not in result.error


@pytest.mark.parametrize("field", ["app_id", "app_secret", "receiver_open_id"])
def test_credentials_must_be_complete_and_without_whitespace(field: str) -> None:
    values = {
        "app_id": "cli_test_app",
        "app_secret": "secret_test_value",
        "receiver_open_id": "ou_test_receiver",
    }
    values[field] = ""

    with pytest.raises(ValueError, match="Feishu"):
        FeishuNotifier(**values)
