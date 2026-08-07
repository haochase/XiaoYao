from __future__ import annotations

import base64
import binascii
import json
import struct
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import ValidationError

try:
    import websocket
except ImportError:  # pragma: no cover - exercised only without the optional client
    class _MissingWebSocket:
        @staticmethod
        def create_connection(*args, **kwargs):
            raise ImportError("websocket-client is required for realtime runtime")

    websocket = _MissingWebSocket()

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.models import TaskCreate
from companion_gateway.voice.runtime import ModelResponse


class ModelRuntimeError(RuntimeError):
    """Raised when the configured model runtime cannot complete a turn."""


class MinicpmOHttpRuntime:
    """Provider-neutral HTTP adapter for an Ascend-hosted MiniCPM-o service."""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MiniCPM-o endpoint must be an absolute HTTP URL")
        if parsed.username or parsed.password:
            raise ValueError("MiniCPM-o endpoint must not contain userinfo")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        if pcm.sample_rate != 16_000:
            raise ModelRuntimeError("MiniCPM-o input sample_rate must be 16000")

        request_payload = {
            "sample_rate": 16_000,
            "channels": 1,
            "format": "pcm_s16le",
            "audio_base64": base64.b64encode(pcm.payload).decode("ascii"),
        }
        request = Request(
            self._endpoint,
            data=json.dumps(request_payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise ModelRuntimeError("MiniCPM-o request failed") from exc

        if status < 200 or status >= 300:
            raise ModelRuntimeError(f"MiniCPM-o returned HTTP {status}")
        return self._decode_response(body)

    @staticmethod
    def _decode_response(body: bytes) -> ModelResponse:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelRuntimeError("MiniCPM-o response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ModelRuntimeError("MiniCPM-o response must be a JSON object")

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ModelRuntimeError("MiniCPM-o response text is required")
        if payload.get("sample_rate") != 24_000:
            raise ModelRuntimeError(
                "MiniCPM-o response sample_rate must be 24000"
            )
        if payload.get("channels") != 1 or payload.get("format") != "pcm_s16le":
            raise ModelRuntimeError("MiniCPM-o response audio format is invalid")

        encoded_audio = payload.get("audio_base64")
        if not isinstance(encoded_audio, str) or not encoded_audio:
            raise ModelRuntimeError("MiniCPM-o response audio is required")
        try:
            audio = base64.b64decode(encoded_audio, validate=True)
            pcm = Pcm16Mono(sample_rate=24_000, payload=audio)
        except (binascii.Error, ValueError) as exc:
            raise ModelRuntimeError("MiniCPM-o response audio is invalid") from exc
        raw_task = payload.get("task")
        task = None
        if raw_task is not None:
            try:
                task = TaskCreate.model_validate(raw_task)
            except ValidationError as exc:
                raise ModelRuntimeError("MiniCPM-o response task is invalid") from exc
        return ModelResponse(text=text, pcm=pcm, task=task)


def _pcm16_to_float32(pcm: Pcm16Mono) -> bytes:
    samples = struct.unpack(f"<{pcm.sample_count}h", pcm.payload)
    return struct.pack(
        f"<{len(samples)}f",
        *(sample / 32_768.0 for sample in samples),
    )


def _float32_to_pcm16(payload: bytes) -> Pcm16Mono:
    if not payload or len(payload) % 4:
        raise ModelRuntimeError("MiniCPM-o audio delta is not float32 PCM")
    sample_count = len(payload) // 4
    samples = struct.unpack(f"<{sample_count}f", payload)
    pcm_samples = [
        max(-32_768, min(32_767, round(sample * 32_767)))
        for sample in samples
    ]
    return Pcm16Mono(
        sample_rate=24_000,
        payload=struct.pack(f"<{sample_count}h", *pcm_samples),
    )


class MinicpmORealtimeRuntime:
    """Half-duplex client for the official MiniCPM-o audio realtime API."""

    def __init__(
        self,
        *,
        endpoint: str,
        auth_token: str | None = None,
        timeout_seconds: float = 20.0,
        system_prompt: str | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ValueError("MiniCPM-o realtime endpoint must be a ws:// URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError(
                "MiniCPM-o realtime endpoint must not contain credentials"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if "mode=audio" not in parsed.query:
            separator = "&" if parsed.query else "?"
            endpoint = f"{endpoint}{separator}mode=audio"
        self._endpoint = endpoint
        self._auth_token = auth_token
        self._timeout_seconds = timeout_seconds
        self._system_prompt = system_prompt

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        if pcm.sample_rate != 16_000:
            raise ModelRuntimeError(
                "MiniCPM-o realtime input sample_rate must be 16000"
            )
        headers = []
        if self._auth_token:
            headers.append(f"Authorization: Bearer {self._auth_token}")
        try:
            socket = websocket.create_connection(
                self._endpoint,
                timeout=self._timeout_seconds,
                header=headers,
            )
            try:
                self._wait_for(socket, "session.queue_done")
                payload = {}
                if self._system_prompt:
                    payload["system_prompt"] = self._system_prompt
                self._send(socket, {"type": "session.init", "payload": payload})
                self._wait_for(socket, "session.created")
                self._send(
                    socket,
                    {
                        "type": "input.append",
                        "input": {
                            "audio": base64.b64encode(_pcm16_to_float32(pcm)).decode(
                                "ascii"
                            ),
                            "force_listen": False,
                        },
                    },
                )
                text_parts: list[str] = []
                audio_parts: list[Pcm16Mono] = []
                task = None
                while True:
                    event = self._receive(socket)
                    event_type = event.get("type")
                    if event_type == "error":
                        raise ModelRuntimeError(
                            "MiniCPM-o realtime service returned an error"
                        )
                    if event_type == "session.closed":
                        break
                    if event_type != "response.output.delta":
                        continue
                    kind = event.get("kind")
                    if kind == "text" and isinstance(event.get("text"), str):
                        text_parts.append(event["text"])
                    elif kind == "audio" and isinstance(event.get("audio"), str):
                        try:
                            audio_parts.append(
                                _float32_to_pcm16(
                                    base64.b64decode(event["audio"], validate=True)
                                )
                            )
                        except (binascii.Error, ValueError) as exc:
                            raise ModelRuntimeError(
                                "MiniCPM-o realtime audio is invalid"
                            ) from exc
                    elif kind == "task":
                        try:
                            task = TaskCreate.model_validate(event.get("task"))
                        except ValidationError as exc:
                            raise ModelRuntimeError(
                                "MiniCPM-o realtime task is invalid"
                            ) from exc
                    elif kind == "listen" and audio_parts:
                        break
                if not audio_parts:
                    raise ModelRuntimeError("MiniCPM-o realtime response has no audio")
                response_pcm = Pcm16Mono(
                    sample_rate=24_000,
                    payload=b"".join(part.payload for part in audio_parts),
                )
                return ModelResponse(
                    text="".join(text_parts) or "MiniCPM-o response",
                    pcm=response_pcm,
                    task=task,
                )
            finally:
                try:
                    self._send(socket, {"type": "session.close", "reason": "user_stop"})
                except Exception:
                    pass
                socket.close()
        except ModelRuntimeError:
            raise
        except Exception as exc:
            raise ModelRuntimeError("MiniCPM-o realtime request failed") from exc

    @staticmethod
    def _send(socket, event: dict[str, object]) -> None:
        socket.send(json.dumps(event, separators=(",", ":")))

    @staticmethod
    def _receive(socket) -> dict[str, object]:
        raw = socket.recv()
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelRuntimeError(
                "MiniCPM-o realtime event is not valid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise ModelRuntimeError("MiniCPM-o realtime event must be an object")
        return event

    def _wait_for(self, socket, expected_type: str) -> dict[str, object]:
        while True:
            event = self._receive(socket)
            event_type = event.get("type")
            if event_type == "error":
                raise ModelRuntimeError("MiniCPM-o realtime service returned an error")
            if event_type == expected_type:
                return event
