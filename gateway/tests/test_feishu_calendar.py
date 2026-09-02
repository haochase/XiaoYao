import json
from datetime import UTC, datetime
from urllib.error import HTTPError

import pytest

from companion_gateway.meeting.feishu import FeishuCalendarClient, FeishuCalendarError


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


OWNER_OPEN_ID = "ou_owner_secret"
CALENDAR_ID = "cal_secret"
EVENT_ID = "event_secret"
DESCRIPTION = "private meeting description"


def token_response(
    *,
    token: str = "tenant_secret",
    expire: int = 7200,
) -> dict[str, object]:
    return {"code": 0, "tenant_access_token": token, "expire": expire}


def user_token_response(
    *,
    access_token: str = "user_access_two",
    refresh_token: str = "user_refresh_two",
    expires_in: int = 7200,
) -> dict[str, object]:
    return {
        "code": 0,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
    }


def calendar_response() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "calendars": [
                {
                    "calendar": {
                        "calendar_id": CALENDAR_ID,
                        "type": "primary",
                        "role": "reader",
                    },
                    "user_id": OWNER_OPEN_ID,
                }
            ]
        },
    }


def event_response(item: dict[str, object]) -> dict[str, object]:
    return {"code": 0, "data": {"items": [item]}}


def timed_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": EVENT_ID,
        "summary": "产品周会",
        "description": DESCRIPTION,
        "start_time": {"timestamp": "1787803800", "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": "1787807400", "timezone": "Asia/Shanghai"},
        "location": {"name": "3A 会议室"},
        "status": "confirmed",
        "attendees": [{"user_id": OWNER_OPEN_ID, "rsvp_status": "accept"}],
    }
    event.update(overrides)
    return event


def make_client(urlopen, **overrides: object) -> FeishuCalendarClient:
    options: dict[str, object] = {
        "app_id": "cli_test",
        "app_secret": "secret",
        "base_url": "https://open.feishu.test",
        "timeout_seconds": 1,
        "max_retries": 2,
        "retry_backoff_seconds": 0.25,
        "urlopen": urlopen,
        "sleep": lambda _seconds: None,
        "monotonic": lambda: 100.0,
    }
    options.update(overrides)
    return FeishuCalendarClient(**options)


def list_events(client: FeishuCalendarClient):
    return client.list_upcoming(
        owner_open_id=OWNER_OPEN_ID,
        start_at=datetime(2026, 8, 27, 4, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 28, 4, 0, tzinfo=UTC),
    )


def test_client_uses_primarys_for_owner_and_returns_sanitized_event() -> None:
    requests = []
    responses = iter([token_response(), calendar_response(), event_response(timed_event())])

    def open_request(request, *, timeout):
        requests.append((request.full_url, request.method, request.data, timeout))
        return FakeResponse(next(responses))

    events = list_events(make_client(open_request))

    assert requests[1][0].endswith("/open-apis/calendar/v4/calendars/primarys?user_id_type=open_id")
    assert requests[1][1] == "POST"
    assert b'"user_ids":["ou_owner_secret"]' in requests[1][2]
    assert "/calendars/primary?" not in requests[1][0]
    assert requests[2][1] == "GET"
    assert events[0].summary == "产品周会"
    assert events[0].fingerprint != EVENT_ID
    assert EVENT_ID not in repr(events[0])
    assert OWNER_OPEN_ID not in repr(events[0])


def test_client_uses_owner_user_token_and_configured_calendar_directly(tmp_path) -> None:
    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        return FakeResponse(event_response(timed_event()))

    client = make_client(
        open_request,
        owner_user_access_token="user_access_one",
        owner_refresh_token="user_refresh_one",
        owner_calendar_id=CALENDAR_ID,
        user_token_state_path=tmp_path / "feishu-user-token.json",
    )

    events = list_events(client)

    assert len(requests) == 1
    assert f"/calendars/{CALENDAR_ID}/events?" in requests[0].full_url
    assert "/calendars/primarys" not in requests[0].full_url
    assert "/tenant_access_token/" not in requests[0].full_url
    assert requests[0].get_header("Authorization") == "Bearer user_access_one"
    assert events[0].summary == "产品周会"


def test_client_refreshes_expired_owner_token_and_atomically_persists_rotation(
    tmp_path,
) -> None:
    requests = []
    state_path = tmp_path / "feishu-user-token.json"
    responses = iter(
        [
            FakeResponse(
                {"code": 99991677, "msg": "private provider text"},
                status=401,
            ),
            FakeResponse(user_token_response()),
            FakeResponse(event_response(timed_event())),
        ]
    )

    def open_request(request, *, timeout):
        requests.append(request)
        return next(responses)

    client = make_client(
        open_request,
        owner_user_access_token="user_access_one",
        owner_refresh_token="user_refresh_one",
        owner_calendar_id=CALENDAR_ID,
        user_token_state_path=state_path,
        oauth_base_url="https://accounts.feishu.test",
        wall_clock=lambda: 100.0,
    )

    events = list_events(client)

    assert events[0].summary == "产品周会"
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert requests[1].full_url == "https://accounts.feishu.test/oauth/v3/token"
    refresh_payload = json.loads(requests[1].data)
    assert refresh_payload == {
        "grant_type": "refresh_token",
        "client_id": "cli_test",
        "client_secret": "secret",
        "refresh_token": "user_refresh_one",
    }
    assert requests[1].get_header("Authorization") is None
    assert requests[2].get_header("Authorization") == "Bearer user_access_two"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "access_token": "user_access_two",
        "refresh_token": "user_refresh_two",
        "access_token_expires_at": 7300.0,
    }
    assert not state_path.with_suffix(".json.tmp").exists()


def test_client_prefers_rotated_owner_tokens_from_state_after_restart(tmp_path) -> None:
    state_path = tmp_path / "feishu-user-token.json"
    state_path.write_text(
        json.dumps(
            {
                "access_token": "user_access_rotated",
                "refresh_token": "user_refresh_rotated",
                "access_token_expires_at": 7300.0,
            }
        ),
        encoding="utf-8",
    )
    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        return FakeResponse(event_response(timed_event()))

    client = make_client(
        open_request,
        owner_user_access_token="stale_env_access",
        owner_refresh_token="stale_env_refresh",
        owner_calendar_id=CALENDAR_ID,
        user_token_state_path=state_path,
        wall_clock=lambda: 100.0,
    )

    list_events(client)

    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer user_access_rotated"


def test_client_parses_all_day_cancelled_event() -> None:
    all_day = timed_event(
        start_time={"date": "2026-08-27"},
        end_time={"date": "2026-08-28"},
        status="cancelled",
    )
    responses = iter([token_response(), calendar_response(), event_response(all_day)])

    events = list_events(make_client(lambda _request, *, timeout: FakeResponse(next(responses))))

    assert events[0].is_all_day is True
    assert events[0].status == "cancelled"
    assert events[0].start_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert events[0].end_at == datetime(2026, 8, 28, tzinfo=UTC)


def test_client_normalizes_unknown_event_status_and_owner_rsvp() -> None:
    unknown_values = timed_event(
        status="future_status",
        attendees=[{"user_id": OWNER_OPEN_ID, "rsvp_status": "future_rsvp"}],
    )
    responses = iter([token_response(), calendar_response(), event_response(unknown_values)])

    events = list_events(make_client(lambda _request, *, timeout: FakeResponse(next(responses))))

    assert events[0].status == "tentative"
    assert events[0].rsvp_status == "unknown"


@pytest.mark.parametrize("location_present", [False, True])
def test_client_defaults_absent_or_null_location_to_empty_string(location_present: bool) -> None:
    event = timed_event()
    if location_present:
        event["location"] = None
    else:
        del event["location"]
    responses = iter([token_response(), calendar_response(), event_response(event)])

    events = list_events(make_client(lambda _request, *, timeout: FakeResponse(next(responses))))

    assert events[0].location == ""


def test_client_reuses_cached_token_before_refresh_threshold() -> None:
    now = [0.0]
    requests = []
    responses = iter(
        [
            token_response(token="tenant-one", expire=120),
            calendar_response(),
            event_response(timed_event()),
            calendar_response(),
            event_response(timed_event()),
        ]
    )

    def open_request(request, *, timeout):
        requests.append(request)
        return FakeResponse(next(responses))

    client = make_client(open_request, monotonic=lambda: now[0])
    list_events(client)
    now[0] = 59.0
    list_events(client)

    token_requests = [
        request
        for request in requests
        if "/tenant_access_token/" in request.full_url
    ]
    assert len(token_requests) == 1


def test_client_refreshes_cached_token_at_refresh_threshold() -> None:
    now = [0.0]
    requests = []
    responses = iter(
        [
            token_response(token="tenant-one", expire=120),
            calendar_response(),
            event_response(timed_event()),
            token_response(token="tenant-two", expire=120),
            calendar_response(),
            event_response(timed_event()),
        ]
    )

    def open_request(request, *, timeout):
        requests.append(request)
        return FakeResponse(next(responses))

    client = make_client(open_request, monotonic=lambda: now[0])
    list_events(client)
    now[0] = 60.0
    list_events(client)

    token_requests = [
        request
        for request in requests
        if "/tenant_access_token/" in request.full_url
    ]
    event_authorizations = [
        request.get_header("Authorization")
        for request in requests
        if "/events?" in request.full_url
    ]
    assert len(token_requests) == 2
    assert event_authorizations == ["Bearer tenant-one", "Bearer tenant-two"]


@pytest.mark.parametrize(
    "truncation",
    [
        {"has_more": True},
        {"has_more": False, "page_token": "private_page_token"},
    ],
)
def test_client_rejects_a_truncated_range_without_returning_partial_events(
    truncation: dict[str, object],
) -> None:
    requests = []
    event_page = {
        "code": 0,
        "data": {"items": [timed_event()], **truncation},
    }
    responses = iter([token_response(), calendar_response(), event_page])

    def open_request(request, *, timeout):
        requests.append(request)
        return FakeResponse(next(responses))

    with pytest.raises(
        FeishuCalendarError,
        match="^event_window_truncated$",
    ) as error:
        list_events(make_client(open_request))

    event_url = requests[2].full_url
    assert "start_time=" in event_url
    assert "end_time=" in event_url
    assert "page_token=" not in event_url
    assert len(requests) == 3
    assert EVENT_ID not in str(error.value)
    assert "private_page_token" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403])
def test_client_does_not_retry_unauthorized_http_failures(status: int) -> None:
    calls = 0

    def open_request(_request, *, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse(b'{"secret":"private"}', status=status)

    with pytest.raises(FeishuCalendarError, match=rf"^http_{status}$") as error:
        list_events(make_client(open_request))

    assert calls == 1
    assert "private" not in str(error.value)


def test_client_retries_only_rate_limit_and_server_errors_within_budget() -> None:
    calls = 0
    sleeps: list[float] = []

    def open_request(_request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            return FakeResponse({}, status=429)
        if calls == 3:
            return FakeResponse({}, status=503)
        if calls == 4:
            return FakeResponse(calendar_response())
        if calls == 5:
            return FakeResponse(event_response(timed_event()))
        return FakeResponse(token_response())

    client = make_client(open_request, sleep=sleeps.append)
    events = list_events(client)

    assert events[0].summary == "产品周会"
    assert calls == 5
    assert sleeps == [0.25, 0.5]


def test_client_redacts_provider_code_malformed_json_and_transport_failures() -> None:
    secret_body = b'{"code":999,"msg":"private meeting description event_secret"}'

    with pytest.raises(FeishuCalendarError, match="^provider_code_999$") as provider_error:
        list_events(make_client(lambda _request, *, timeout: FakeResponse(secret_body)))
    with pytest.raises(FeishuCalendarError, match="^response_invalid_json$") as json_error:
        list_events(make_client(lambda _request, *, timeout: FakeResponse(b"not json")))

    def raise_transport(request, *, timeout):
        raise HTTPError(request.full_url, 400, "event_secret", {}, None)

    with pytest.raises(FeishuCalendarError, match="^http_400$") as http_error:
        list_events(make_client(raise_transport))

    for error in (provider_error.value, json_error.value, http_error.value):
        rendered = repr(error)
        assert EVENT_ID not in rendered
        assert OWNER_OPEN_ID not in rendered
        assert CALENDAR_ID not in rendered
        assert DESCRIPTION not in rendered
        assert "tenant_secret" not in rendered


def test_client_retries_transport_errors_only_within_budget() -> None:
    calls = 0
    sleeps: list[float] = []

    def open_request(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise OSError("network unavailable")
        return FakeResponse(token_response())

    with pytest.raises(FeishuCalendarError, match="^transport_error$"):
        list_events(make_client(open_request, sleep=sleeps.append))

    assert calls == 3
    assert sleeps == [0.25, 0.5]
