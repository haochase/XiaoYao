from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from companion_gateway.domain.vision import VisionContentType, VisionObservation
from companion_gateway.storage.sqlite import SQLiteTaskRepository


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]

_EXTENSIONS: dict[VisionContentType, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class VisionFeatureDisabled(RuntimeError):
    pass


class VisionConsentRequired(ValueError):
    pass


class VisionUnsupportedType(ValueError):
    pass


class VisionTooLarge(ValueError):
    pass


class VisionQuotaExceeded(ValueError):
    pass


class VisionDuplicateTurn(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class VisionObservationService:
    def __init__(
        self,
        *,
        repository: SQLiteTaskRepository,
        storage_path: str | Path,
        enabled: bool = False,
        max_upload_bytes: int = 10_000_000,
        retention_days: int = 7,
        quota_bytes: int = 200_000_000,
        clock: Clock = _utc_now,
        id_factory: IdFactory = _new_id,
    ) -> None:
        if max_upload_bytes <= 0 or max_upload_bytes > 10_000_000:
            raise ValueError("max_upload_bytes must be between 1 and 10000000")
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        self._repository = repository
        self._storage_path = Path(storage_path)
        self._enabled = enabled
        self._max_upload_bytes = max_upload_bytes
        self._retention_days = retention_days
        self._quota_bytes = quota_bytes
        self._clock = clock
        self._id_factory = id_factory

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise VisionFeatureDisabled("Vision feature is disabled")

    def upload(
        self,
        *,
        subject_id: str,
        turn_id: str,
        content_type: str,
        payload: bytes,
        captured_at: datetime | None = None,
        consent: bool = True,
    ) -> VisionObservation:
        self._ensure_enabled()
        if not consent:
            raise VisionConsentRequired("vision upload requires explicit consent")
        normalized_type = self._normalize_content_type(content_type)
        self._validate_signature(normalized_type, payload)
        byte_size = len(payload)
        if byte_size == 0 or byte_size > self._max_upload_bytes:
            raise VisionTooLarge("vision image exceeds the upload limit")
        current = _require_aware(captured_at or self._clock())
        self.purge(now=current)
        if self._repository.get_vision_observation_for_turn(
            subject_id=subject_id,
            turn_id=turn_id,
            now=current,
        ) is not None:
            raise VisionDuplicateTurn("one image is already bound to this voice turn")
        if (
            self._repository.vision_usage_bytes(subject_id=subject_id, now=current)
            + byte_size
            > self._quota_bytes
        ):
            raise VisionQuotaExceeded("vision media quota exceeded")

        observation_id = self._id_factory("vision")
        observation = VisionObservation(
            observation_id=observation_id,
            subject_id=subject_id,
            turn_id=turn_id,
            captured_at=current,
            expires_at=current + timedelta(days=self._retention_days),
            content_type=normalized_type,
            byte_size=byte_size,
            sha256=sha256(payload).hexdigest(),
            storage_key=f"{observation_id}.{_EXTENSIONS[normalized_type]}",
        )
        self._storage_path.mkdir(parents=True, exist_ok=True)
        file_path = self._storage_path / observation.storage_key
        file_path.write_bytes(payload)
        try:
            stored, created = self._repository.create_vision_observation(observation)
        except Exception:
            file_path.unlink(missing_ok=True)
            raise
        if not created:
            file_path.unlink(missing_ok=True)
            raise VisionDuplicateTurn("one image is already bound to this voice turn")
        return stored

    def get(
        self,
        *,
        subject_id: str,
        observation_id: str,
        now: datetime | None = None,
    ) -> VisionObservation | None:
        self._ensure_enabled()
        current = _require_aware(now or self._clock())
        observation = self._repository.get_vision_observation(observation_id)
        if (
            observation is None
            or observation.subject_id != subject_id
            or observation.expires_at.astimezone(UTC) <= current
        ):
            return None
        return observation

    def list(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> list[VisionObservation]:
        self._ensure_enabled()
        current = _require_aware(now or self._clock())
        return self._repository.list_vision_observations(
            subject_id=subject_id,
            now=current,
        )

    def delete(self, *, subject_id: str, observation_id: str) -> bool:
        self._ensure_enabled()
        observation = self._repository.get_vision_observation(observation_id)
        if observation is None or observation.subject_id != subject_id:
            return False
        deleted = self._repository.delete_vision_observation(
            subject_id=subject_id,
            observation_id=observation_id,
        )
        if deleted:
            (self._storage_path / observation.storage_key).unlink(missing_ok=True)
        return deleted

    def purge(self, *, now: datetime | None = None) -> int:
        self._ensure_enabled()
        current = _require_aware(now or self._clock())
        storage_keys = self._repository.purge_expired_vision_observations(now=current)
        for storage_key in storage_keys:
            (self._storage_path / storage_key).unlink(missing_ok=True)
        return len(storage_keys)

    @staticmethod
    def _normalize_content_type(content_type: str) -> VisionContentType:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in _EXTENSIONS:
            raise VisionUnsupportedType("vision image type is not supported")
        return normalized  # type: ignore[return-value]

    @staticmethod
    def _validate_signature(content_type: VisionContentType, payload: bytes) -> None:
        valid = {
            "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
            "image/webp": len(payload) >= 12
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP",
        }
        if not valid[content_type]:
            raise VisionUnsupportedType("vision image signature does not match type")
