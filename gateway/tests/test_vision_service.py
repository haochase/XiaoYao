from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.domain.vision import VisionObservation
from companion_gateway.storage.sqlite import SQLiteTaskRepository
from companion_gateway.vision.service import (
    VisionDuplicateTurn,
    VisionObservationService,
    VisionQuotaExceeded,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-pixels"


def create_service(tmp_path, *, quota_bytes=200):
    repository = SQLiteTaskRepository(tmp_path / "vision.db")
    repository.initialize()
    ids = iter(["vision-1", "vision-2", "vision-3"])
    return VisionObservationService(
        repository=repository,
        storage_path=tmp_path / "vision-files",
        enabled=True,
        max_upload_bytes=100,
        retention_days=7,
        quota_bytes=quota_bytes,
        clock=lambda: NOW,
        id_factory=lambda _prefix: next(ids),
    ), repository


def test_upload_persists_metadata_and_local_file(tmp_path) -> None:
    service, repository = create_service(tmp_path)

    observation = service.upload(
        subject_id="family-1",
        turn_id="turn-1",
        content_type="image/png",
        payload=PNG,
    )

    assert observation.byte_size == len(PNG)
    assert observation.sha256
    assert (tmp_path / "vision-files" / observation.storage_key).read_bytes() == PNG
    assert service.get(subject_id="family-1", observation_id="vision-1") == observation
    assert repository.list_vision_observations(subject_id="family-1", now=NOW) == [
        observation
    ]


def test_one_turn_accepts_only_one_active_image_and_quota_is_bounded(tmp_path) -> None:
    service, _repository = create_service(tmp_path, quota_bytes=len(PNG))
    service.upload(
        subject_id="family-1",
        turn_id="turn-1",
        content_type="image/png",
        payload=PNG,
    )

    with pytest.raises(VisionDuplicateTurn):
        service.upload(
            subject_id="family-1",
            turn_id="turn-1",
            content_type="image/png",
            payload=PNG,
        )
    with pytest.raises(VisionQuotaExceeded):
        service.upload(
            subject_id="family-1",
            turn_id="turn-2",
            content_type="image/png",
            payload=PNG,
        )


def test_expired_observation_is_purged_with_file_and_subject_delete_is_scoped(
    tmp_path,
) -> None:
    service, _repository = create_service(tmp_path)
    observation = service.upload(
        subject_id="family-1",
        turn_id="turn-1",
        content_type="image/png",
        payload=PNG,
        captured_at=NOW - timedelta(days=8),
    )
    assert service.get(subject_id="family-2", observation_id=observation.observation_id) is None
    assert service.delete(subject_id="family-2", observation_id=observation.observation_id) is False

    assert service.purge(now=NOW) == 1
    assert not (tmp_path / "vision-files" / observation.storage_key).exists()
