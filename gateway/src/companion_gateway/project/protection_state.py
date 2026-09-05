from __future__ import annotations

from companion_gateway.project.protection import ContentProtector
from companion_gateway.project.snapshot_loader import (
    ProjectSnapshotHydrator,
    SnapshotHydrationError,
)
from companion_gateway.project.sync_repository import (
    ProjectSyncRepository,
    StoredProjectGeneration,
    SyncConflict,
)


class ProtectionStateError(RuntimeError):
    pass


def initialize_repository_protection(
    repository: ProjectSyncRepository,
    protector: ContentProtector,
    *,
    identity_digest: str,
) -> None:
    protector_version = getattr(protector, "protector_version", None)
    if not isinstance(protector_version, str) or not protector_version:
        raise ProtectionStateError("protection_version_invalid")
    try:
        repository.configure_protection(
            identity_digest,
            protector_version,
        )
        return
    except SyncConflict as exc:
        if str(exc) != "protection_metadata_missing":
            raise ProtectionStateError(str(exc)) from None

    hydrator = ProjectSnapshotHydrator(protector)
    verified: list[StoredProjectGeneration] = []
    try:
        for project_id in repository.list_active_project_ids():
            generation = repository.load_active_generation(project_id)
            if generation is None:
                raise ProtectionStateError("protection_identity_unverified")
            hydrator.snapshot(generation)
            verified.append(generation)
        repository.adopt_verified_protection(
            identity_digest,
            protector_version,
            tuple(verified),
        )
    except (
        ProtectionStateError,
        RuntimeError,
        SnapshotHydrationError,
        SyncConflict,
        TypeError,
        ValueError,
    ):
        raise ProtectionStateError("protection_identity_unverified") from None


__all__ = [
    "ProtectionStateError",
    "initialize_repository_protection",
]
