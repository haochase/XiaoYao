from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen as default_urlopen

from companion_gateway.meeting.models import MeetingEvent


class FeishuCalendarError(RuntimeError):
    """Calendar provider failure with a deliberately redacted public message."""


class FeishuCalendarClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        owner_user_access_token: str | None = None,
        owner_refresh_token: str | None = None,
        owner_calendar_id: str | None = None,
        user_token_state_path: str | Path | None = None,
        oauth_base_url: str = "https://accounts.feishu.cn",
        urlopen: Callable[..., Any] = default_urlopen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._require_token_value(app_id, "app_id")
        self._require_token_value(app_secret, "app_secret")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Feishu base URL must be an absolute HTTP URL")
        if timeout_seconds <= 0:
            raise ValueError("Feishu timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("Feishu max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("Feishu retry_backoff_seconds must not be negative")
        owner_values = (
            owner_user_access_token,
            owner_refresh_token,
            owner_calendar_id,
        )
        if any(value is not None for value in owner_values) and not all(
            value is not None for value in owner_values
        ):
            raise ValueError("Feishu owner user credentials must be configured together")
        if (
            all(value is not None for value in owner_values)
            and user_token_state_path is None
        ):
            raise ValueError("Feishu user_token_state_path is required for owner user mode")
        parsed_oauth = urlparse(oauth_base_url)
        if parsed_oauth.scheme not in {"http", "https"} or not parsed_oauth.netloc:
            raise ValueError("Feishu OAuth base URL must be an absolute HTTP URL")
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._oauth_base_url = oauth_base_url.rstrip("/")
        self._urlopen = urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._token_lock = Lock()
        self._tenant_access_token: str | None = None
        self._token_expires_at = 0.0
        self._owner_user_access_token = owner_user_access_token
        self._owner_refresh_token = owner_refresh_token
        self._owner_calendar_id = owner_calendar_id
        self._user_token_state_path = (
            None if user_token_state_path is None else Path(user_token_state_path)
        )
        self._owner_access_expires_at: float | None = None
        if self._owner_user_access_token is not None:
            self._load_owner_user_token_state()

    def list_upcoming(
        self,
        *,
        owner_open_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MeetingEvent, ...]:
        self._require_token_value(owner_open_id, "owner_open_id")
        self._require_aware_range(start_at, end_at)
        if self._owner_user_access_token is not None:
            return self._list_upcoming_as_owner(
                owner_open_id=owner_open_id,
                start_at=start_at,
                end_at=end_at,
            )
        token = self._get_tenant_access_token()
        primary = self._request_json(
            method="POST",
            path="/open-apis/calendar/v4/calendars/primarys?user_id_type=open_id",
            payload={"user_ids": [owner_open_id]},
            token=token,
        )
        calendars = self._nested_list(primary, "data", "calendars")
        calendar_id = self._find_primary_calendar(calendars, owner_open_id)
        if calendar_id is None:
            raise FeishuCalendarError("primary_calendar_unavailable")
        query = urlencode(
            {
                "start_time": int(start_at.timestamp()),
                "end_time": int(end_at.timestamp()),
                "page_size": 500,
                "user_id_type": "open_id",
            }
        )
        events = self._request_json(
            method="GET",
            path=(
                "/open-apis/calendar/v4/calendars/"
                f"{quote(calendar_id, safe='')}/events?{query}"
            ),
            payload=None,
            token=token,
        )
        self._require_complete_event_window(events)
        return tuple(
            self._parse_event(item, owner_open_id)
            for item in self._nested_list(events, "data", "items")
        )

    def _list_upcoming_as_owner(
        self,
        *,
        owner_open_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[MeetingEvent, ...]:
        calendar_id = self._owner_calendar_id
        if calendar_id is None:
            raise FeishuCalendarError("owner_calendar_id_missing")
        query = urlencode(
            {
                "start_time": int(start_at.timestamp()),
                "end_time": int(end_at.timestamp()),
                "page_size": 500,
                "user_id_type": "open_id",
            }
        )
        path = (
            "/open-apis/calendar/v4/calendars/"
            f"{quote(calendar_id, safe='')}/events?{query}"
        )
        token = self._get_owner_user_access_token()
        try:
            events = self._request_json(
                method="GET",
                path=path,
                payload=None,
                token=token,
            )
        except FeishuCalendarError as exc:
            if str(exc) != "provider_code_99991677":
                raise
            token = self._refresh_owner_user_access_token(stale_token=token)
            events = self._request_json(
                method="GET",
                path=path,
                payload=None,
                token=token,
            )
        self._require_complete_event_window(events)
        return tuple(
            self._parse_event(item, owner_open_id)
            for item in self._nested_list(events, "data", "items")
        )

    def _get_tenant_access_token(self) -> str:
        with self._token_lock:
            if (
                self._tenant_access_token is not None
                and self._monotonic() < self._token_expires_at
            ):
                return self._tenant_access_token
            response = self._request_json(
                method="POST",
                path="/open-apis/auth/v3/tenant_access_token/internal",
                payload={"app_id": self._app_id, "app_secret": self._app_secret},
                token=None,
            )
            token = response.get("tenant_access_token")
            if not isinstance(token, str) or not token.strip():
                raise FeishuCalendarError("tenant_access_token_missing")
            try:
                expires_seconds = float(
                    response.get("expire", response.get("expires_in", 0))
                )
            except (TypeError, ValueError):
                raise FeishuCalendarError("tenant_access_token_expiry_invalid") from None
            self._tenant_access_token = token
            self._token_expires_at = self._monotonic() + max(
                1.0,
                expires_seconds - 60.0,
            )
            return token

    def _get_owner_user_access_token(self) -> str:
        with self._token_lock:
            token = self._owner_user_access_token
            if token is None:
                raise FeishuCalendarError("owner_user_access_token_missing")
            if (
                self._owner_access_expires_at is not None
                and self._wall_clock() >= self._owner_access_expires_at - 60.0
            ):
                return self._refresh_owner_user_token_locked()
            return token

    def _refresh_owner_user_access_token(self, *, stale_token: str) -> str:
        with self._token_lock:
            if (
                self._owner_user_access_token is not None
                and self._owner_user_access_token != stale_token
            ):
                return self._owner_user_access_token
            return self._refresh_owner_user_token_locked()

    def _refresh_owner_user_token_locked(self) -> str:
        refresh_token = self._owner_refresh_token
        if refresh_token is None:
            raise FeishuCalendarError("owner_refresh_token_missing")
        if self._user_token_state_path is None:
            raise FeishuCalendarError("user_token_state_path_missing")
        response = self._request_json(
            method="POST",
            path="/oauth/v3/token",
            payload={
                "grant_type": "refresh_token",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "refresh_token": refresh_token,
            },
            token=None,
            base_url=self._oauth_base_url,
        )
        access_token = response.get("access_token")
        rotated_refresh_token = response.get("refresh_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise FeishuCalendarError("owner_user_access_token_missing")
        if (
            not isinstance(rotated_refresh_token, str)
            or not rotated_refresh_token.strip()
        ):
            raise FeishuCalendarError("owner_refresh_token_missing")
        try:
            expires_in = float(response.get("expires_in", 0))
        except (TypeError, ValueError):
            raise FeishuCalendarError("owner_user_access_token_expiry_invalid") from None
        if expires_in <= 0:
            raise FeishuCalendarError("owner_user_access_token_expiry_invalid")
        expires_at = self._wall_clock() + expires_in
        self._persist_owner_user_token_state(
            access_token=access_token,
            refresh_token=rotated_refresh_token,
            access_token_expires_at=expires_at,
        )
        self._owner_user_access_token = access_token
        self._owner_refresh_token = rotated_refresh_token
        self._owner_access_expires_at = expires_at
        return access_token

    def _load_owner_user_token_state(self) -> None:
        path = self._user_token_state_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            expires_at = float(payload["access_token_expires_at"])
            self._require_token_value(access_token, "owner_user_access_token")
            self._require_token_value(refresh_token, "owner_refresh_token")
            if expires_at <= 0:
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise FeishuCalendarError("user_token_state_invalid") from None
        self._owner_user_access_token = access_token
        self._owner_refresh_token = refresh_token
        self._owner_access_expires_at = expires_at

    def _persist_owner_user_token_state(
        self,
        *,
        access_token: str,
        refresh_token: str,
        access_token_expires_at: float,
    ) -> None:
        path = self._user_token_state_path
        if path is None:
            raise FeishuCalendarError("user_token_state_path_missing")
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_expires_at": access_token_expires_at,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except OSError:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise FeishuCalendarError("user_token_state_write_failed") from None

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        token: str | None,
        base_url: str | None = None,
    ) -> dict[str, object]:
        data = (
            None
            if payload is None
            else json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(self._max_retries + 1):
            request = Request(
                f"{base_url or self._base_url}{path}",
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with self._urlopen(request, timeout=self._timeout_seconds) as response:
                    status = int(getattr(response, "status", 200))
                    body = response.read()
            except HTTPError as exc:
                if self._should_retry(exc.code, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                provider_error = self._provider_error_from_body(exc.read())
                if provider_error is not None:
                    raise FeishuCalendarError(provider_error) from None
                raise FeishuCalendarError(f"http_{self._safe_status(exc.code)}") from None
            except (URLError, OSError, TimeoutError):
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise FeishuCalendarError("transport_error") from None
            if status < 200 or status >= 300:
                if self._should_retry(status, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                provider_error = self._provider_error_from_body(body)
                if provider_error is not None:
                    raise FeishuCalendarError(provider_error)
                raise FeishuCalendarError(f"http_{self._safe_status(status)}")
            try:
                decoded = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                raise FeishuCalendarError("response_invalid_json") from None
            if not isinstance(decoded, dict):
                raise FeishuCalendarError("response_invalid_object")
            if decoded.get("code") != 0:
                raise FeishuCalendarError(self._provider_error_label(decoded.get("code")))
            return decoded
        raise FeishuCalendarError("retry_budget_exhausted")

    @staticmethod
    def _parse_event(item: object, owner_open_id: str) -> MeetingEvent:
        if not isinstance(item, dict):
            raise FeishuCalendarError("event_invalid")
        try:
            raw_id = str(item["event_id"])
            start = FeishuCalendarClient._mapping(item.get("start_time"))
            end = FeishuCalendarClient._mapping(item.get("end_time"))
            all_day = bool(start.get("date")) and not start.get("timestamp")
            start_at = FeishuCalendarClient._parse_event_time(start, all_day)
            end_at = FeishuCalendarClient._parse_event_time(end, all_day)
            owner = next(
                (
                    attendee
                    for attendee in item.get("attendees", [])
                    if isinstance(attendee, dict)
                    and attendee.get("user_id") == owner_open_id
                ),
                None,
            )
            rsvp = (
                str(owner.get("rsvp_status", "unknown")).lower()
                if isinstance(owner, dict)
                else "unknown"
            )
            if rsvp not in {
                "accept",
                "tentative",
                "decline",
                "needs_action",
                "removed",
            }:
                rsvp = "unknown"
            status = str(item.get("status") or "tentative").lower()
            if status not in {"confirmed", "tentative", "cancelled"}:
                status = "tentative"
            location_value = item.get("location")
            location = (
                {}
                if location_value is None
                else FeishuCalendarClient._mapping(location_value)
            )
            return MeetingEvent(
                fingerprint=sha256(raw_id.encode("utf-8")).hexdigest(),
                summary=str(item.get("summary") or "未命名会议").strip()[:1000],
                description_excerpt=str(item.get("description") or "")[:1000],
                start_at=start_at,
                end_at=end_at,
                location=str(location.get("name") or "")[:512],
                status=status,
                rsvp_status=rsvp,
                is_all_day=all_day,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise FeishuCalendarError("event_invalid") from None

    @staticmethod
    def _parse_event_time(value: dict[str, object], all_day: bool) -> datetime:
        if all_day:
            return datetime.fromisoformat(str(value["date"])).replace(tzinfo=UTC)
        return datetime.fromtimestamp(int(str(value["timestamp"])), tz=UTC)

    @staticmethod
    def _find_primary_calendar(
        calendars: list[object],
        owner_open_id: str,
    ) -> str | None:
        for item in calendars:
            if not isinstance(item, dict) or item.get("user_id") != owner_open_id:
                continue
            calendar = FeishuCalendarClient._mapping(item.get("calendar"))
            if (
                calendar.get("type") == "primary"
                and calendar.get("role") in {"reader", "writer", "owner"}
                and isinstance(calendar.get("calendar_id"), str)
                and calendar["calendar_id"].strip()
            ):
                return calendar["calendar_id"]
        return None

    @staticmethod
    def _nested_list(response: dict[str, object], *keys: str) -> list[object]:
        value: object = response
        for key in keys:
            if not isinstance(value, dict):
                raise FeishuCalendarError("response_invalid_payload")
            value = value.get(key)
        if not isinstance(value, list):
            raise FeishuCalendarError("response_invalid_payload")
        return value

    @staticmethod
    def _require_complete_event_window(response: dict[str, object]) -> None:
        data = response.get("data")
        if not isinstance(data, dict):
            raise FeishuCalendarError("response_invalid_payload")
        if data.get("has_more") is True or bool(data.get("page_token")):
            raise FeishuCalendarError("event_window_truncated")

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("mapping required")
        return value

    def _should_retry(self, status: int, attempt: int) -> bool:
        return (
            (status == 429 or 500 <= status <= 599)
            and attempt < self._max_retries
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _safe_status(value: object) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return "unknown"

    @staticmethod
    def _provider_error_label(code: object) -> str:
        if isinstance(code, int) and not isinstance(code, bool):
            return f"provider_code_{code}"
        return "provider_error"

    @staticmethod
    def _provider_error_from_body(body: object) -> str | None:
        try:
            decoded = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or decoded.get("code") in {None, 0}:
            return None
        return FeishuCalendarClient._provider_error_label(decoded.get("code"))

    @staticmethod
    def _require_aware_range(start_at: datetime, end_at: datetime) -> None:
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError("start_at must be timezone-aware")
        if end_at.tzinfo is None or end_at.utcoffset() is None:
            raise ValueError("end_at must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be later than start_at")

    @staticmethod
    def _require_token_value(value: str, name: str) -> None:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"Feishu {name} must be non-empty")
        if any(character.isspace() for character in value):
            raise ValueError(f"Feishu {name} must not contain whitespace")
