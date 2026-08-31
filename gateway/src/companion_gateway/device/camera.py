from __future__ import annotations

import hashlib
import re
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CameraCaptureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["camera"]
    state: Literal["start"]
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    content_type: Literal["image/jpeg"]
    declared_bytes: int = Field(gt=0, le=2_097_152)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ValueError("sha256 must be a hexadecimal digest")
        return value.lower()


class CameraUploadError(ValueError):
    pass


class CameraUploadState:
    def __init__(self, *, max_bytes: int = 2_097_152) -> None:
        if max_bytes <= 0 or max_bytes > 2_097_152:
            raise ValueError("camera max_bytes must be between 1 and 2097152")
        self._max_bytes = max_bytes
        self._metadata: CameraCaptureMetadata | None = None
        self._buffer = bytearray()

    @property
    def active(self) -> bool:
        return self._metadata is not None

    @property
    def complete(self) -> bool:
        return self._metadata is not None and len(self._buffer) == self._metadata.declared_bytes

    @property
    def metadata(self) -> CameraCaptureMetadata | None:
        return self._metadata

    def start(self, metadata: CameraCaptureMetadata) -> None:
        if self.active:
            raise CameraUploadError("camera upload already active")
        if metadata.declared_bytes > self._max_bytes:
            raise CameraUploadError("camera image exceeds configured limit")
        self._metadata = metadata
        self._buffer.clear()

    def accept_chunk(self, payload: bytes) -> None:
        if self._metadata is None:
            raise CameraUploadError("camera upload is not active")
        if not isinstance(payload, bytes) or not payload:
            self._reset()
            raise CameraUploadError("camera image chunk is empty")
        if len(self._buffer) + len(payload) > self._max_bytes:
            self._reset()
            raise CameraUploadError("camera image exceeds configured limit")
        if len(self._buffer) + len(payload) > self._metadata.declared_bytes:
            self._reset()
            raise CameraUploadError("camera image exceeds declared length")
        self._buffer.extend(payload)

    def finish(self) -> bytes:
        metadata = self._metadata
        if metadata is None:
            raise CameraUploadError("camera upload is not active")
        payload = bytes(self._buffer)
        try:
            if len(payload) != metadata.declared_bytes:
                raise CameraUploadError("camera image length does not match metadata")
            if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
                raise CameraUploadError("camera image is not a valid JPEG")
            digest = hashlib.sha256(payload).hexdigest()
            if digest != metadata.sha256:
                raise CameraUploadError("camera image hash does not match metadata")
            return payload
        finally:
            self._reset()

    def _reset(self) -> None:
        self._metadata = None
        self._buffer.clear()


class CameraFrameRegistry:
    """Bounded in-memory handoff from a device upload to vision processing."""

    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], bytes] = {}
        self._lock = RLock()

    def put(self, *, session_id: str, turn_id: str, payload: bytes) -> None:
        with self._lock:
            self._frames[(session_id, turn_id)] = bytes(payload)

    def get(self, *, session_id: str, turn_id: str) -> bytes | None:
        with self._lock:
            payload = self._frames.get((session_id, turn_id))
            return bytes(payload) if payload is not None else None

    def pop(self, *, session_id: str, turn_id: str) -> bytes | None:
        with self._lock:
            payload = self._frames.pop((session_id, turn_id), None)
            return bytes(payload) if payload is not None else None

    def clear_session(self, *, session_id: str) -> None:
        with self._lock:
            for key in tuple(self._frames):
                if key[0] == session_id:
                    del self._frames[key]


def build_vision_capability_message(
    explain_url: str,
    *,
    session_id: str = "ses-camera",
) -> dict[str, object]:
    if not explain_url.strip():
        raise ValueError("vision explain URL must not be empty")
    if not session_id.strip():
        raise ValueError("vision session_id must not be empty")
    return {
        "type": "mcp",
        "session_id": session_id,
        "payload": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "vision": {
                        "url": explain_url,
                    }
                }
            },
        },
    }


def derive_vision_explain_url(public_websocket_url: str) -> str:
    parsed = urlsplit(public_websocket_url.strip())
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("public websocket URL must use ws or wss")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("public websocket URL must not contain credentials or query")
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "/v1/vision/explain", "", ""))
