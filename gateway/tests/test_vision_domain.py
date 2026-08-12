from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion_gateway.domain.vision import VisionObservation


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def valid_observation(**overrides):
    payload = {
        "observation_id": "vision-1",
        "subject_id": "family-1",
        "turn_id": "turn-1",
        "captured_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "content_type": "image/png",
        "byte_size": 16,
        "sha256": "a" * 64,
        "storage_key": "vision-1.png",
    }
    payload.update(overrides)
    return VisionObservation.model_validate(payload)


def test_vision_observation_requires_expiry_and_safe_metadata() -> None:
    observation = valid_observation()

    assert observation.content_type == "image/png"
    assert observation.byte_size == 16

    with pytest.raises(ValidationError):
        valid_observation(expires_at=NOW)
    with pytest.raises(ValidationError):
        valid_observation(sha256="not-a-digest")
    with pytest.raises(ValidationError):
        valid_observation(content_type="image/svg+xml")
    with pytest.raises(ValidationError):
        valid_observation(extra="forbidden")


def test_vision_observation_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        valid_observation(captured_at=datetime(2026, 8, 12, 12, 0))
