from __future__ import annotations

import json
import time
from collections.abc import Callable
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen as default_urlopen

from companion_gateway.domain.medication import FeishuSendResult


class FeishuProviderError(RuntimeError):
    """Provider failure with a deliberately redacted public message."""


class FeishuNotifier:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        receiver_open_id: str,
        base_url: str = "https://open.feishu.cn",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        urlopen: Callable[..., Any] = default_urlopen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._require_token_value(app_id, "app_id")
        self._require_token_value(app_secret, "app_secret")
        self._require_token_value(receiver_open_id, "receiver_open_id")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Feishu base URL must be an absolute HTTP URL")
        if timeout_seconds <= 0:
            raise ValueError("Feishu timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("Feishu max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("Feishu retry_backoff_seconds must not be negative")
        self._app_id = app_id
        self._app_secret = app_secret
        self._receiver_open_id = receiver_open_id
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._urlopen = urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._token_lock = Lock()
        self._tenant_access_token: str | None = None
        self._token_expires_at = 0.0

    def send_text(
        self,
        *,
        text: str,
        trace_id: str,
        open_id: str | None = None,
    ) -> FeishuSendResult:
        if not text.strip():
            raise ValueError("Feishu message text must not be empty")
        receiver = open_id or self._receiver_open_id
        self._require_token_value(receiver, "receiver_open_id")
        try:
            token = self._get_tenant_access_token()
            response = self._request_json(
                path="/open-apis/im/v1/messages?receive_id_type=open_id",
                payload={
                    "receive_id": receiver,
                    "msg_type": "text",
                    "content": json.dumps(
                        {"text": text},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except FeishuProviderError as exc:
            return FeishuSendResult(success=False, error=str(exc))
        code = response.get("code")
        if code != 0:
            return FeishuSendResult(
                success=False,
                error=f"provider_code_{code}",
            )
        data = response.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            return FeishuSendResult(success=False, error="message_id_missing")
        return FeishuSendResult(success=True, message_id=message_id)

    def _get_tenant_access_token(self) -> str:
        with self._token_lock:
            if (
                self._tenant_access_token is not None
                and self._monotonic() < self._token_expires_at
            ):
                return self._tenant_access_token
            response = self._request_json(
                path="/open-apis/auth/v3/tenant_access_token/internal",
                payload={"app_id": self._app_id, "app_secret": self._app_secret},
                headers={},
            )
            code = response.get("code")
            if code != 0:
                raise FeishuProviderError(f"provider_code_{code}")
            token = response.get("tenant_access_token")
            if not isinstance(token, str) or not token.strip():
                raise FeishuProviderError("tenant_access_token_missing")
            expires = response.get("expire", response.get("expires_in", 0))
            try:
                expires_seconds = float(expires)
            except (TypeError, ValueError) as exc:
                raise FeishuProviderError("tenant_access_token_expiry_invalid") from exc
            self._tenant_access_token = token
            self._token_expires_at = self._monotonic() + max(
                1.0,
                expires_seconds - 60.0,
            )
            return token

    def _request_json(
        self,
        *,
        path: str,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, object]:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        for attempt in range(self._max_retries + 1):
            request = Request(
                f"{self._base_url}{path}",
                data=encoded_payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **headers,
                },
                method="POST",
            )
            try:
                with self._urlopen(request, timeout=self._timeout_seconds) as response:
                    status = int(getattr(response, "status", 200))
                    body = response.read()
            except HTTPError as exc:
                status = exc.code
                if self._should_retry(status, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                raise FeishuProviderError(f"http_{status}") from exc
            except (URLError, OSError, TimeoutError) as exc:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise FeishuProviderError("transport_error") from exc
            if status < 200 or status >= 300:
                if self._should_retry(status, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                raise FeishuProviderError(f"http_{status}")
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FeishuProviderError("response_invalid_json") from exc
            if not isinstance(decoded, dict):
                raise FeishuProviderError("response_invalid_object")
            return decoded
        raise FeishuProviderError("retry_budget_exhausted")

    def _should_retry(self, status: int, attempt: int) -> bool:
        return (status == 429 or 500 <= status <= 599) and attempt < self._max_retries

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _require_token_value(value: str, name: str) -> None:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"Feishu {name} must be non-empty")
        if any(character.isspace() for character in value):
            raise ValueError(f"Feishu {name} must not contain whitespace")
