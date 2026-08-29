from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from companion_gateway.domain.models import ContentText, Identifier


RecentChannel = Literal["feishu"]


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class RecentChannelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: Identifier
    subject_id: Identifier
    channel: RecentChannel
    external_message_id: Identifier
    content: ContentText
    created_at: datetime
    expires_at: datetime

    _validate_created_at = field_validator("created_at")(_require_aware)
    _validate_expires_at = field_validator("expires_at")(_require_aware)

    @model_validator(mode="after")
    def validate_expiry_window(self) -> "RecentChannelMessage":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self
