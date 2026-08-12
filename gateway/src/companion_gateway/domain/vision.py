from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.domain.models import Identifier


VisionContentType = Literal["image/jpeg", "image/png", "image/webp"]


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class VisionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: Identifier
    subject_id: Identifier
    turn_id: Identifier
    captured_at: datetime
    expires_at: datetime
    content_type: VisionContentType
    byte_size: int = Field(gt=0, le=10_000_000)
    sha256: str = Field(min_length=64, max_length=64)
    storage_key: Identifier

    _validate_captured_at = field_validator("captured_at", "expires_at")(
        _require_timezone_aware
    )

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value.lower()):
            raise ValueError("sha256 must be a hexadecimal digest")
        return value.lower()

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("storage_key must be a file name")
        return value

    @model_validator(mode="after")
    def validate_expiry_window(self) -> "VisionObservation":
        if self.expires_at <= self.captured_at:
            raise ValueError("expires_at must be later than captured_at")
        return self
