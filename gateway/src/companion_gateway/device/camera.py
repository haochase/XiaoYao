from __future__ import annotations

import hashlib
import re
from typing import Literal

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
