from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock

from companion_gateway.project.index import (
    ProjectRuntimeSnapshot,
    ProjectSnapshotRegistry,
)
from companion_gateway.project.clock_guard import ProjectClockGuard
from companion_gateway.project.models import EvidenceRef
from companion_gateway.project.protection import ContentProtector
from companion_gateway.project.snapshot_loader import (
    ProjectSnapshotHydrator,
    SnapshotHydrationError,
)
from companion_gateway.project.sync_models import (
    RetrievalRequest,
    SourceSyncStatus,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import (
    ProjectSyncRepository,
    SyncConflict,
)
from companion_gateway.project.sync_service import ProjectSourceUnavailable


class RepositoryBackedProjectQueryFacade:
    def __init__(
        self,
        repository: ProjectSyncRepository,
        protector: ContentProtector,
        *,
        identity_digest: Callable[[], str],
        source_freshness_seconds: int,
        sync_interval_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(identity_digest):
            raise TypeError("identity_digest must be callable")
        if (
            not isinstance(source_freshness_seconds, int)
            or isinstance(source_freshness_seconds, bool)
            or not 60 <= source_freshness_seconds <= 86_400
        ):
            raise ValueError("source_freshness_seconds_invalid")
        version = getattr(protector, "protector_version", None)
        if not isinstance(version, str) or not version:
            raise ValueError("protector_version_missing")
        self._repository = repository
        self._protector_version = version
        self._identity_digest = identity_digest
        self._source_freshness_seconds = source_freshness_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._clock_guard = ProjectClockGuard(
            repository,
            sync_interval_seconds=sync_interval_seconds,
            monotonic=monotonic,
        )
        self._registry = ProjectSnapshotRegistry()
        self._hydrator = ProjectSnapshotHydrator(protector)
        self._cache_keys: dict[str, tuple[str, int]] = {}
        self._protection_configured = False
        self._lock = RLock()

    def refresh(
        self,
        project_id: str,
    ) -> ProjectRuntimeSnapshot | None:
        with self._lock:
            try:
                self._clock_guard.check(wall_now=self._clock())
                if (
                    self._repository.protection_descriptor() is not None
                    or self._repository.has_active_generation(project_id)
                ):
                    self._configure_protection()
                active = self._repository.load_active_generation(project_id)
                if active is None:
                    self._cache_keys.pop(project_id, None)
                    self._registry.remove(project_id)
                    return None
                key = (active.generation_id, active.source_cursor)
                cached = self._registry.get(project_id)
                if cached is not None and self._cache_keys.get(project_id) == key:
                    return cached
                snapshot = self._hydrator.snapshot(active)
            except (
                RuntimeError,
                SnapshotHydrationError,
                SyncConflict,
                ValueError,
            ):
                self._cache_keys.pop(project_id, None)
                self._registry.remove(project_id)
                raise ProjectSourceUnavailable("source_unavailable") from None
            self._registry.swap(project_id, snapshot)
            self._cache_keys[project_id] = key
            return snapshot

    def get(self, project_id: str) -> ProjectRuntimeSnapshot | None:
        return self.refresh(project_id)

    def require_sources_fresh(
        self,
        project_id: str,
        source_refs: tuple[EvidenceRef, ...],
        *,
        now: datetime,
    ) -> None:
        _require_aware(now)
        snapshot = self.refresh(project_id)
        if snapshot is None:
            return
        if self._repository.load_clock_state().clock_untrusted:
            raise ProjectSourceUnavailable("clock_untrusted")
        states = {
            (item.source_type, item.source_id_hash): item
            for item in snapshot.source_states
        }
        sources = {
            (item.source_type, item.source_id_hash): item
            for item in snapshot.sources
        }
        for reference in source_refs:
            try:
                source_type = SyncSourceType(reference.source_type)
            except ValueError:
                raise ProjectSourceUnavailable("source_unavailable") from None
            source_hash = _source_id_hash(reference.source_id)
            key = (source_type, source_hash)
            state = states.get(key)
            source = sources.get(key)
            if (
                reference.permission_scope != snapshot.context.permission_scope
                or state is None
                or source is None
                or source.source_id != reference.source_id
                or state.status
                in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED}
                or state.last_success_at is None
            ):
                raise ProjectSourceUnavailable("source_unavailable")
            if (now - state.last_success_at).total_seconds() > (
                self._source_freshness_seconds
            ):
                raise ProjectSourceUnavailable("source_stale")

    def save_retrieval_request(
        self,
        request: RetrievalRequest,
    ) -> RetrievalRequest:
        if self.refresh(request.project_id) is None:
            raise ProjectSourceUnavailable("project_not_synced")
        if self._repository.load_clock_state().clock_untrusted:
            raise ProjectSourceUnavailable("clock_untrusted")
        return self._repository.save_retrieval_request(request)

    def _configure_protection(self) -> None:
        if self._protection_configured:
            return
        self._repository.configure_protection(
            self._identity_digest(),
            self._protector_version,
        )
        self._protection_configured = True


def _source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectSourceUnavailable("source_unavailable")


__all__ = ["RepositoryBackedProjectQueryFacade"]
