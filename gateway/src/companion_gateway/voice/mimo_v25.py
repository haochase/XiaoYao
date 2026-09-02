from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.memory import MemoryProposalCandidate
from companion_gateway.domain.models import TaskCreate
from companion_gateway.voice.minicpm_o import ModelRuntimeError
from companion_gateway.voice.runtime import ModelResponse, VoiceAction, VoiceIntent


logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)
_INPUT_SAMPLE_RATE = 16_000
_OUTPUT_SAMPLE_RATE = 24_000
_RETRYABLE_STATUS_MIN = 500
_RETRYABLE_STATUS_MAX = 599
_MAX_MEMORY_PROPOSALS = 3
_DEFAULT_SYSTEM_PROMPT = (
    "You are the XiaoYao voice companion. Reply in Chinese when the user speaks "
    "Chinese. Return exactly one JSON object with keys reply, task, action, and intent. "
    "The reply "
    "value must be a short natural spoken response. The task value must be null "
    "unless the user explicitly requests a reminder or device action. If a task "
    "is needed, task must match the gateway TaskCreate schema. The action value "
    "must be null unless the user explicitly confirms a medication occurrence or "
    "asks to disable the medication plan; supported action types are "
    "acknowledge_medication_occurrence and disable_medication_plan."
    " The intent value must be null unless the user asks for the current time, "
    "current date, current date and time, or latest reminder status. For those "
    "queries return exactly one of current_time, current_date, current_datetime, "
    "or reminder_status as {\"type\": \"...\"}."
    " If the user asks about their next or upcoming meeting, return intent "
    "{\"type\":\"next_meeting\"} and an empty reply; do not answer meeting facts "
    "yourself; the gateway will ground them from Feishu Calendar."
)
_MEMORY_PROPOSAL_PROMPT = (
    " Optionally return memory_proposals as a list only when the user explicitly "
    "states a stable preference. Allowed categories are address, "
    "reminder_preference, routine_preference, and approved_fact. Do not propose "
    "credentials, health data, location, device data, or raw conversation text."
)
_SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _pcm_to_wav_data_url(pcm: Pcm16Mono) -> str:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(pcm.sample_rate)
        wav.writeframes(pcm.payload)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:audio/wav;base64,{encoded}"


def _text_content(message: object) -> str:
    if not isinstance(message, dict):
        raise ModelRuntimeError("MiMo chat response message is invalid")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    raise ModelRuntimeError("MiMo chat response text is required")


def _first_message(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ModelRuntimeError("MiMo response must be a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelRuntimeError("MiMo response choices are required")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ModelRuntimeError("MiMo response choice is invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelRuntimeError("MiMo response message is required")
    return message


def _decode_json_response(
    text: str,
) -> tuple[
    str,
    TaskCreate | None,
    VoiceAction | None,
    VoiceIntent | None,
    tuple[MemoryProposalCandidate, ...],
]:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[-1][:-3].strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return text, None, None, None, ()
    if not isinstance(payload, dict):
        return text, None, None, None, ()
    raw_intent = payload.get("intent")
    intent = None
    if raw_intent is not None:
        try:
            intent = VoiceIntent.model_validate(raw_intent)
        except ValidationError as exc:
            raise ModelRuntimeError("MiMo response intent is invalid") from exc
    raw_reply = payload.get("reply")
    if isinstance(raw_reply, str) and raw_reply.strip():
        reply = raw_reply.strip()
    elif intent is not None:
        reply = ""
    else:
        return text, None, None, None, ()
    raw_task = payload.get("task")
    task = None
    if raw_task is not None:
        try:
            task = TaskCreate.model_validate(raw_task)
        except ValidationError as exc:
            raise ModelRuntimeError("MiMo response task is invalid") from exc
    raw_action = payload.get("action")
    action = None
    if raw_action is not None:
        try:
            action = VoiceAction.model_validate(raw_action)
        except ValidationError as exc:
            raise ModelRuntimeError("MiMo response action is invalid") from exc
    proposals: list[MemoryProposalCandidate] = []
    raw_proposals = payload.get("memory_proposals")
    if isinstance(raw_proposals, list):
        for raw_proposal in raw_proposals:
            if len(proposals) >= _MAX_MEMORY_PROPOSALS:
                break
            try:
                proposals.append(MemoryProposalCandidate.model_validate(raw_proposal))
            except ValidationError:
                continue
    return reply, task, action, intent, tuple(proposals)


class MimoV25Runtime:
    """MiMo-V2.5 audio-understanding plus MiMo TTS runtime."""

    def __init__(
        self,
        *,
        openai_base_url: str,
        api_key: str,
        model: str = "mimo-v2.5",
        tts_model: str = "mimo-v2.5-tts",
        tts_voice: str = "mimo_default",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        system_prompt: str | None = None,
        memory_proposals_enabled: bool = False,
        clock: Callable[[], datetime] = _utc_now,
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
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in api_key
        ):
            raise ValueError("MiMo API key must be a non-empty token")
        if not model.strip() or not tts_model.strip() or not tts_voice.strip():
            raise ValueError("MiMo model settings must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("MiMo timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("MiMo max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("MiMo retry_backoff_seconds must not be negative")
        if not isinstance(memory_proposals_enabled, bool):
            raise ValueError("memory_proposals_enabled must be a boolean")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._base_url = openai_base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._clock = clock
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        if memory_proposals_enabled:
            self._system_prompt += _MEMORY_PROPOSAL_PROMPT
        self._action_context = ""
        self._memory_context = ""

    def set_action_context(
        self,
        *,
        occurrence_ids: tuple[str, ...],
        plan_ids: tuple[str, ...],
    ) -> None:
        self._action_context = (
            "\nGateway-managed medication context for this turn: "
            f"occurrence_ids={list(occurrence_ids)!r}; "
            f"plan_ids={list(plan_ids)!r}. Return an action ID only from these lists."
        )

    def set_memory_context(self, context: str) -> None:
        if not isinstance(context, str):
            raise ValueError("memory context must be text")
        self._memory_context = context

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        if pcm.sample_rate != _INPUT_SAMPLE_RATE:
            raise ModelRuntimeError(
                f"MiMo input sample_rate must be {_INPUT_SAMPLE_RATE}"
            )
        current_time = self._clock().astimezone(_SHANGHAI_TIMEZONE).isoformat()
        time_context = (
            "\nCurrent gateway time: "
            f"{current_time} (Asia/Shanghai). Use this as the authoritative "
            "current time when the user asks for the date or time. Always match "
            "the user's requested precision exactly: if the user asks for only "
            "hour and minute, do not include the year, month, day, seconds, or "
            "weekday."
        )
        started_at = time.perf_counter()
        chat_message = self._request(
            {
                "model": self._model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            self._system_prompt
                            + time_context
                            + self._action_context
                            + self._memory_context
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": _pcm_to_wav_data_url(pcm),
                                    "format": "wav",
                                },
                            },
                            {
                                "type": "text",
                                "text": "Understand the audio and answer the user.",
                            },
                        ],
                    },
                ],
                "max_completion_tokens": 1_024,
                "thinking": {"type": "disabled"},
                "stream": False,
            }
        )
        chat_finished_at = time.perf_counter()
        reply, task, action, intent, memory_proposals = _decode_json_response(
            _text_content(chat_message)
        )
        if intent is None:
            tts_started_at = time.perf_counter()
            response_pcm = self.synthesize(reply)
            finished_at = time.perf_counter()
        else:
            tts_started_at = chat_finished_at
            response_pcm = None
            finished_at = chat_finished_at
        logger.info(
            "mimo_voice_turn_completed model=%s chat_duration_ms=%s "
            "tts_duration_ms=%s total_duration_ms=%s output_audio_ms=%s",
            self._model,
            round((chat_finished_at - started_at) * 1_000),
            round((finished_at - tts_started_at) * 1_000),
            round((finished_at - started_at) * 1_000),
            round(response_pcm.duration_ms) if response_pcm is not None else 0,
        )
        return ModelResponse(
            text=reply,
            pcm=response_pcm,
            task=task,
            action=action,
            intent=intent,
            memory_proposals=memory_proposals,
        )

    def synthesize(self, text: str) -> Pcm16Mono:
        if not text.strip():
            raise ModelRuntimeError("MiMo TTS text is required")
        tts_message = self._request(
            {
                "model": self._tts_model,
                "messages": [{"role": "assistant", "content": text}],
                "audio": {"format": "pcm16", "voice": self._tts_voice},
                "stream": False,
            }
        )
        audio = tts_message.get("audio")
        if not isinstance(audio, dict) or not isinstance(audio.get("data"), str):
            raise ModelRuntimeError("MiMo TTS response audio is required")
        try:
            encoded = audio["data"]
            if "," in encoded and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            output_pcm = base64.b64decode(encoded, validate=True)
            response_pcm = Pcm16Mono(
                sample_rate=_OUTPUT_SAMPLE_RATE,
                payload=output_pcm,
            )
        except (binascii.Error, ValueError) as exc:
            raise ModelRuntimeError("MiMo TTS response audio is invalid") from exc
        return response_pcm

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        for attempt in range(self._max_retries + 1):
            request = Request(
                f"{self._base_url}/chat/completions",
                data=encoded_payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "api-key": self._api_key,
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    status = getattr(response, "status", 200)
                    body = response.read()
            except HTTPError as exc:
                status = exc.code
                if self._should_retry(status, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                raise ModelRuntimeError(f"MiMo returned HTTP {status}") from exc
            except (URLError, OSError, TimeoutError) as exc:
                raise ModelRuntimeError("MiMo request failed") from exc
            if status < 200 or status >= 300:
                if self._should_retry(status, attempt):
                    self._sleep_before_retry(attempt)
                    continue
                raise ModelRuntimeError(f"MiMo returned HTTP {status}")
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelRuntimeError("MiMo response is not valid JSON") from exc
            if not isinstance(decoded, dict):
                raise ModelRuntimeError("MiMo response must be a JSON object")
            return _first_message(decoded)
        raise ModelRuntimeError("MiMo retry budget exhausted")

    def _should_retry(self, status: int, attempt: int) -> bool:
        retryable = status == 429 or (
            _RETRYABLE_STATUS_MIN <= status <= _RETRYABLE_STATUS_MAX
        )
        return retryable and attempt < self._max_retries

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)
