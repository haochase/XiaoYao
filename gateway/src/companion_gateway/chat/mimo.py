from __future__ import annotations

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from companion_gateway.chat.service import TextChatTurn
from companion_gateway.voice.minicpm_o import ModelRuntimeError


logger = logging.getLogger("uvicorn.error")
_SYSTEM_PROMPT = (
    "You are XiaoYao, a concise and warm companion assistant. Always identify "
    "yourself as XiaoYao. Reply in Chinese when the user writes Chinese. Return "
    "plain text only, keep the answer suitable for chat, and never claim that an "
    "external action was completed unless the gateway supplied its result."
)


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ModelRuntimeError("MiMo response must be a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelRuntimeError("MiMo response choices are required")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ModelRuntimeError("MiMo response message is required")
    content = choice["message"].get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = "".join(parts).strip()
        if text:
            return text
    raise ModelRuntimeError("MiMo response text is required")


class MimoTextChatRuntime:
    def __init__(
        self,
        *,
        openai_base_url: str,
        api_key: str,
        model: str = "mimo-v2.5",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> None:
        parsed = urlparse(openai_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MiMo OpenAI base URL must be an absolute URL")
        if not api_key or api_key != api_key.strip() or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in api_key
        ):
            raise ValueError("MiMo API key must be a non-empty token")
        if not model.strip() or not system_prompt.strip():
            raise ValueError("MiMo text chat settings must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("MiMo timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("MiMo max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("MiMo retry_backoff_seconds must not be negative")
        self._base_url = openai_base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._system_prompt = system_prompt

    def respond(
        self,
        text: str,
        *,
        history: tuple[TextChatTurn, ...] = (),
    ) -> str:
        normalized = text.strip()
        if not normalized:
            raise ValueError("MiMo text chat input must not be empty")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt}
        ]
        messages.extend(
            {"role": turn.role, "content": turn.content} for turn in history
        )
        messages.append({"role": "user", "content": normalized})
        payload = {
            "model": self._model,
            "messages": messages,
            "max_completion_tokens": 512,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        started_at = time.perf_counter()
        reply = self._request(payload)
        logger.info(
            "mimo_text_chat_completed model=%s duration_ms=%s history_messages=%s",
            self._model,
            round((time.perf_counter() - started_at) * 1_000),
            len(history),
        )
        return reply

    def _request(self, payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        for attempt in range(self._max_retries + 1):
            request = Request(
                f"{self._base_url}/chat/completions",
                data=encoded,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "api-key": self._api_key,
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    status = int(getattr(response, "status", 200))
                    body = response.read()
            except HTTPError as exc:
                status = exc.code
                if self._should_retry(status, attempt):
                    self._sleep(attempt)
                    continue
                raise ModelRuntimeError(f"MiMo returned HTTP {status}") from exc
            except (URLError, OSError, TimeoutError) as exc:
                raise ModelRuntimeError("MiMo request failed") from exc
            if status < 200 or status >= 300:
                if self._should_retry(status, attempt):
                    self._sleep(attempt)
                    continue
                raise ModelRuntimeError(f"MiMo returned HTTP {status}")
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelRuntimeError("MiMo response is not valid JSON") from exc
            return _response_text(decoded)
        raise ModelRuntimeError("MiMo retry budget exhausted")

    def _should_retry(self, status: int, attempt: int) -> bool:
        return (status == 429 or 500 <= status <= 599) and attempt < self._max_retries

    def _sleep(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)
