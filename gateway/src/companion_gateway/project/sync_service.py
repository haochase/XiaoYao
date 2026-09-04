from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Literal

from companion_gateway.project.auth import (
    ProjectApiAuthenticator,
    ProjectApiPrincipal,
)
from companion_gateway.project.index import (
    EvidenceSource,
    ProjectEvidenceIndex,
    ProjectRuntimeSnapshot,
    ProjectSnapshotRegistry,
)
from companion_gateway.project.models import EvidenceRef
from companion_gateway.project.protection import ContentProtector
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    ProjectSyncHealth,
    ProjectSyncOutcome,
    ProjectSyncStatus,
    SourceErrorType,
    SourceSnapshot,
    SourceState,
    SourceSyncStatus,
    SyncAudit,
    SyncEnvelope,
    SyncResult,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import (
    ProtectedChunkRecord,
    ProtectedSourceRecord,
    ProjectSyncRepository,
    StoredProjectGeneration,
    SyncCommit,
)


class ProjectSyncError(RuntimeError):
    pass


class ProjectSyncValidationError(ProjectSyncError):
    pass


class ProjectSourceUnavailable(ProjectSyncError):
    pass


@dataclass(frozen=True)
class ClockCheckResult:
    immediate_sync_required: bool
    clock_untrusted: bool
    reason: Literal["normal", "resume_detected", "clock_rollback"]


def compute_envelope_content_hash(envelope: SyncEnvelope) -> str:
    if not isinstance(envelope, SyncEnvelope):
        raise TypeError("envelope must be a SyncEnvelope")
    payload = {
        "schema_version": envelope.schema_version,
        "project_id": envelope.project_id,
        "producer": envelope.producer,
        "context": envelope.context.model_dump(
            mode="json",
            exclude={"generated_at"},
        ),
        "sources": sorted(
            (_semantic_source(source) for source in envelope.sources),
            key=lambda item: (
                item["source_type"],
                item["source_id"],
            ),
        ),
        "tombstones": sorted(
            (
                tombstone.model_dump(mode="json", exclude={"occurred_at"})
                for tombstone in envelope.tombstones
            ),
            key=lambda item: (
                item["source_type"],
                item["source_id"],
            ),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_source(source: SourceSnapshot) -> dict[str, object]:
    payload = source.model_dump(
        mode="json",
        exclude={
            "chunks",
            "error_type",
            "fetched_at",
            "retry_after_seconds",
            "retryable",
        },
    )
    payload["chunks"] = sorted(
        (chunk.model_dump(mode="json") for chunk in source.chunks),
        key=lambda item: (
            item["source_id"],
            item["source_version"],
            item["ordinal"],
            item["chunk_id"],
        ),
    )
    return payload


def _require_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProjectSyncValidationError(label)
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset is None:
        raise ProjectSyncValidationError(label)


def _source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _generation_id(envelope: SyncEnvelope) -> str:
    material = (
        envelope.project_id.encode("utf-8")
        + b"\0"
        + envelope.content_hash.encode("ascii")
        + b"\0"
        + str(envelope.source_cursor).encode("ascii")
    )
    return "gen_" + hashlib.sha256(material).hexdigest()[:32]


def _chunk_id(chunk: EvidenceChunk) -> str:
    payload = {
        "end_offset": chunk.end_offset,
        "heading_path": chunk.heading_path,
        "ordinal": chunk.ordinal,
        "source_id": chunk.source_id,
        "start_offset": chunk.start_offset,
        "text": chunk.text,
        "version": chunk.source_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectSyncService:
    def __init__(
        self,
        repository: ProjectSyncRepository,
        protector: ContentProtector,
        registry: ProjectSnapshotRegistry,
        *,
        sync_interval_seconds: float = 300.0,
        source_freshness_seconds: int = 1800,
        clock_skew_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(sync_interval_seconds, (int, float))
            or isinstance(sync_interval_seconds, bool)
            or not math.isfinite(sync_interval_seconds)
            or sync_interval_seconds <= 0
        ):
            raise ValueError("sync_interval_seconds_invalid")
        if (
            not isinstance(source_freshness_seconds, int)
            or isinstance(source_freshness_seconds, bool)
            or not 60 <= source_freshness_seconds <= 86_400
        ):
            raise ValueError("source_freshness_seconds_invalid")
        if (
            not isinstance(clock_skew_seconds, (int, float))
            or isinstance(clock_skew_seconds, bool)
            or not math.isfinite(clock_skew_seconds)
            or not 0 <= clock_skew_seconds <= 300
        ):
            raise ValueError("clock_skew_seconds_invalid")
        if not callable(monotonic):
            raise ValueError("monotonic_invalid")
        if not isinstance(repository, ProjectSyncRepository):
            raise TypeError("repository must be a ProjectSyncRepository")
        if not isinstance(registry, ProjectSnapshotRegistry):
            raise TypeError("registry must be a ProjectSnapshotRegistry")
        if not callable(getattr(protector, "protect", None)) or not callable(
            getattr(protector, "unprotect", None)
        ):
            raise TypeError("protector must be a ContentProtector")

        self._repository = repository
        self._protector = protector
        self._registry = registry
        self._sync_interval_seconds = float(sync_interval_seconds)
        self._source_freshness_seconds = source_freshness_seconds
        self._clock_skew_seconds = float(clock_skew_seconds)
        self._monotonic = monotonic
        self._apply_lock = RLock()
        self._clock_lock = RLock()
        self._last_wall: datetime | None = None
        self._last_monotonic: float | None = None
        self._clock_untrusted = False
        self._immediate_sync_required = False

    def apply(
        self,
        envelope: SyncEnvelope,
        *,
        principal: ProjectApiPrincipal,
        now: datetime,
    ) -> SyncResult:
        if not isinstance(envelope, SyncEnvelope):
            raise ProjectSyncValidationError("invalid_envelope")
        _require_aware(now, "now_must_be_aware")
        if abs((envelope.generated_at - now).total_seconds()) > (
            self._clock_skew_seconds
        ):
            raise ProjectSyncValidationError("clock_skew_exceeded")
        ProjectApiAuthenticator.authorize(
            principal,
            project_id=envelope.project_id,
            permission_scope=envelope.context.permission_scope,
        )
        expected_hash = compute_envelope_content_hash(envelope)
        if not hmac.compare_digest(expected_hash, envelope.content_hash):
            raise ProjectSyncValidationError("content_hash_mismatch")
        baseline_monotonic = self._read_monotonic()

        with self._apply_lock:
            started = time.perf_counter()
            active = self._repository.load_active_generation(envelope.project_id)
            states = self._source_states(envelope, active)
            protected_sources, protected_chunks = self._protect_active_sources(
                envelope
            )
            generated_id = _generation_id(envelope)
            expected_outcome = self._expected_outcome(
                envelope,
                active,
                states,
                generated_id,
            )
            runtime_snapshot = self._candidate_snapshot(
                envelope,
                active,
                states,
                generated_id,
            )
            audit = SyncAudit(
                sync_id=envelope.sync_id,
                project_id=envelope.project_id,
                started_at=now,
                finished_at=now,
                outcome=expected_outcome,
                source_counts_by_status=dict(
                    Counter(item.status for item in states)
                ),
                chunk_count=len(protected_chunks),
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                error_type=None,
            )
            committed = self._repository.commit(
                SyncCommit(
                    envelope=envelope,
                    generation_id=generated_id,
                    source_states=states,
                    protected_sources=protected_sources,
                    protected_chunks=protected_chunks,
                    audit=audit,
                )
            )
            if runtime_snapshot.generation_id != committed.generation_id:
                runtime_snapshot = ProjectRuntimeSnapshot(
                    project_id=runtime_snapshot.project_id,
                    generation_id=committed.generation_id,
                    context=runtime_snapshot.context,
                    source_states=runtime_snapshot.source_states,
                    sources=runtime_snapshot.sources,
                    chunks=runtime_snapshot.chunks,
                    evidence_index=runtime_snapshot.evidence_index,
                )
            self._registry.swap(envelope.project_id, runtime_snapshot)
            self._reset_clock(now, baseline_monotonic)

        current_status = self.status(envelope.project_id, now=now)
        failed_sources = sum(
            item.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}
            for item in states
        )
        return SyncResult(
            sync_id=envelope.sync_id,
            outcome=committed.outcome,
            project_status=current_status.health,
            accepted_sources=len(states) - failed_sources,
            failed_sources=failed_sources,
            generation_id=committed.generation_id,
            next_sync_before=now + timedelta(
                seconds=self._sync_interval_seconds
            ),
        )

    def status(self, project_id: str, *, now: datetime) -> ProjectSyncStatus:
        _require_aware(now, "now_must_be_aware")
        snapshot = self._registry.get(project_id)
        if snapshot is None:
            raise ProjectSourceUnavailable("project_not_synced")
        with self._clock_lock:
            clock_untrusted = self._clock_untrusted

        projected = tuple(
            self._project_state(item, now) for item in snapshot.source_states
        )
        retained_keys = {
            (item.source_type, item.source_id_hash) for item in snapshot.sources
        }
        fresh_decision_source = any(
            state.source_type
            in {SyncSourceType.DOCUMENT, SyncSourceType.MEETING_NOTE}
            and (state.source_type, state.source_id_hash) in retained_keys
            and self._is_fresh(state, now)
            for state in snapshot.source_states
        )
        has_degraded_source = any(
            state.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}
            or self._is_expired(state, now)
            for state in snapshot.source_states
            if state.status
            not in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED}
        )
        if clock_untrusted:
            health = ProjectSyncHealth.CLOCK_UNTRUSTED
        elif not fresh_decision_source:
            health = ProjectSyncHealth.STALE
        elif has_degraded_source:
            health = ProjectSyncHealth.DEGRADED
        else:
            health = ProjectSyncHealth.HEALTHY

        successes = tuple(
            state.last_success_at
            for state in snapshot.source_states
            if state.last_success_at is not None
            and state.status
            not in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED}
        )
        return ProjectSyncStatus(
            project_id=project_id,
            health=health,
            sources=projected,
            last_success_at=max(successes) if successes else None,
            next_sync_before=(
                min(
                    value
                    + timedelta(seconds=self._source_freshness_seconds)
                    for value in successes
                )
                if successes
                else None
            ),
        )

    def require_sources_fresh(
        self,
        project_id: str,
        source_refs: tuple[EvidenceRef, ...],
        *,
        now: datetime,
    ) -> None:
        _require_aware(now, "now_must_be_aware")
        snapshot = self._registry.get(project_id)
        if snapshot is None:
            raise ProjectSourceUnavailable("project_not_synced")
        with self._clock_lock:
            if self._clock_untrusted:
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
            if self._is_expired(state, now):
                raise ProjectSourceUnavailable("source_stale")

    def recheck_clock(
        self,
        *,
        wall_now: datetime,
        monotonic_now: float | None = None,
    ) -> ClockCheckResult:
        _require_aware(wall_now, "wall_now_must_be_aware")
        sample = (
            self._read_monotonic()
            if monotonic_now is None
            else self._validate_monotonic(monotonic_now)
        )
        with self._clock_lock:
            reason: Literal["normal", "resume_detected", "clock_rollback"] = (
                "normal"
            )
            if self._last_wall is not None and self._last_monotonic is not None:
                wall_elapsed = (wall_now - self._last_wall).total_seconds()
                monotonic_elapsed = sample - self._last_monotonic
                if wall_elapsed < -self._clock_skew_seconds:
                    self._clock_untrusted = True
                    self._immediate_sync_required = True
                    reason = "clock_rollback"
                elif monotonic_elapsed > 2 * self._sync_interval_seconds:
                    self._immediate_sync_required = True
                    reason = "resume_detected"
            self._last_wall = wall_now
            self._last_monotonic = sample
            return ClockCheckResult(
                immediate_sync_required=self._immediate_sync_required,
                clock_untrusted=self._clock_untrusted,
                reason=reason,
            )

    def consume_immediate_sync_request(self) -> bool:
        with self._clock_lock:
            pending = self._immediate_sync_required
            self._immediate_sync_required = False
            return pending

    def _source_states(
        self,
        envelope: SyncEnvelope,
        active: StoredProjectGeneration | None,
    ) -> tuple[SourceState, ...]:
        prior = (
            {
                (item.source_type, item.source_id_hash): item
                for item in active.source_states
            }
            if active is not None
            else {}
        )
        states: list[SourceState] = []
        seen_hashes: set[str] = set()
        for source in envelope.sources:
            source_hash = _source_id_hash(source.source_id)
            if source_hash in seen_hashes:
                raise ProjectSyncValidationError("source_identity_conflict")
            seen_hashes.add(source_hash)
            if source.status in {
                SourceSyncStatus.DELETED,
                SourceSyncStatus.REVOKED,
            }:
                raise ProjectSyncValidationError("source_tombstone_required")
            previous = prior.get((source.source_type, source_hash))
            if source.status is SourceSyncStatus.ACTIVE:
                source_version = source.source_version
                content_hash = source.content_hash
                last_success_at = source.fetched_at
                last_error_type = None
            else:
                source_version = (
                    previous.source_version if previous else source.source_version
                )
                content_hash = (
                    previous.content_hash if previous else source.content_hash
                )
                last_success_at = previous.last_success_at if previous else None
                last_error_type = source.error_type
            states.append(
                SourceState(
                    project_id=envelope.project_id,
                    source_type=source.source_type,
                    source_id_hash=source_hash,
                    source_version=source_version,
                    content_hash=content_hash,
                    permission_hash=source.permission_hash,
                    status=source.status,
                    last_attempt_at=source.fetched_at,
                    last_success_at=last_success_at,
                    last_error_type=last_error_type,
                )
            )
        for tombstone in envelope.tombstones:
            source_hash = _source_id_hash(tombstone.source_id)
            if source_hash in seen_hashes:
                raise ProjectSyncValidationError("source_identity_conflict")
            seen_hashes.add(source_hash)
            previous = prior.get((tombstone.source_type, source_hash))
            states.append(
                SourceState(
                    project_id=envelope.project_id,
                    source_type=tombstone.source_type,
                    source_id_hash=source_hash,
                    source_version=None,
                    content_hash=None,
                    permission_hash=(
                        previous.permission_hash
                        if previous is not None
                        else _source_id_hash(tombstone.permission_scope)
                    ),
                    status=tombstone.status,
                    last_attempt_at=tombstone.occurred_at,
                    last_success_at=None,
                    last_error_type=None,
                )
            )
        return tuple(
            sorted(
                states,
                key=lambda item: (item.source_type.value, item.source_id_hash),
            )
        )

    def _protect_active_sources(
        self,
        envelope: SyncEnvelope,
    ) -> tuple[tuple[ProtectedSourceRecord, ...], tuple[ProtectedChunkRecord, ...]]:
        protected_sources: list[ProtectedSourceRecord] = []
        protected_chunks: list[ProtectedChunkRecord] = []
        for source in envelope.sources:
            if source.status is not SourceSyncStatus.ACTIVE:
                continue
            source_hash = _source_id_hash(source.source_id)
            protected_sources.append(
                ProtectedSourceRecord(
                    source_type=source.source_type,
                    source_id_hash=source_hash,
                    protected_source_id=self._protect_text(
                        envelope.project_id,
                        source.source_id,
                    ),
                    protected_title=self._protect_text(
                        envelope.project_id,
                        source.source_title,
                    ),
                    protected_url=self._protect_text(
                        envelope.project_id,
                        source.source_url,
                    ),
                    source_version=source.source_version,
                    source_time=source.source_time,
                    permission_hash=source.permission_hash,
                    content_hash=source.content_hash,
                )
            )
            for chunk in source.chunks:
                if not hmac.compare_digest(_chunk_id(chunk), chunk.chunk_id):
                    raise ProjectSyncValidationError("chunk_id_mismatch")
                if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != (
                    chunk.content_hash
                ):
                    raise ProjectSyncValidationError(
                        "chunk_content_hash_mismatch"
                    )
                protected_chunks.append(
                    ProtectedChunkRecord(
                        chunk_id=chunk.chunk_id,
                        source_type=source.source_type,
                        source_id_hash=source_hash,
                        source_version=chunk.source_version,
                        ordinal=chunk.ordinal,
                        protected_heading_path=self._protector.protect(
                            envelope.project_id,
                            json.dumps(
                                chunk.heading_path,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        ),
                        protected_text=self._protect_text(
                            envelope.project_id,
                            chunk.text,
                        ),
                        start_offset=chunk.start_offset,
                        end_offset=chunk.end_offset,
                        content_hash=chunk.content_hash,
                    )
                )
        return (
            tuple(
                sorted(
                    protected_sources,
                    key=lambda item: (
                        item.source_type.value,
                        item.source_id_hash,
                    ),
                )
            ),
            tuple(
                sorted(
                    protected_chunks,
                    key=lambda item: (
                        item.source_type.value,
                        item.source_id_hash,
                        item.ordinal,
                        item.chunk_id,
                    ),
                )
            ),
        )

    def _protect_text(self, project_id: str, value: str) -> bytes:
        plaintext = value.encode("utf-8")
        if not plaintext:
            raise ProjectSyncValidationError("protected_content_empty")
        return self._protector.protect(project_id, plaintext)

    def _candidate_snapshot(
        self,
        envelope: SyncEnvelope,
        active: StoredProjectGeneration | None,
        states: tuple[SourceState, ...],
        generation_id: str,
    ) -> ProjectRuntimeSnapshot:
        if active is not None and envelope.content_hash == active.content_hash:
            sources, chunks = self._decrypt_generation(active, None)
            runtime_states = self._unchanged_runtime_states(
                envelope,
                active,
                states,
            )
            context = active.context
            runtime_generation_id = active.generation_id
        else:
            retained_keys = {
                (source.source_type, _source_id_hash(source.source_id))
                for source in envelope.sources
                if source.status in {
                    SourceSyncStatus.FAILED,
                    SourceSyncStatus.STALE,
                }
            }
            retained_sources, retained_chunks = self._decrypt_generation(
                active,
                retained_keys,
            )
            current_sources = tuple(
                EvidenceSource(
                    source_type=source.source_type,
                    source_id=source.source_id,
                    source_id_hash=_source_id_hash(source.source_id),
                    source_title=source.source_title,
                    source_url=source.source_url,
                    source_version=source.source_version,
                    source_time=source.source_time,
                    permission_hash=source.permission_hash,
                    content_hash=source.content_hash,
                )
                for source in envelope.sources
                if source.status is SourceSyncStatus.ACTIVE
            )
            current_chunks = tuple(
                chunk
                for source in envelope.sources
                if source.status is SourceSyncStatus.ACTIVE
                for chunk in source.chunks
            )
            sources = current_sources + retained_sources
            chunks = current_chunks + retained_chunks
            runtime_states = states
            context = envelope.context
            runtime_generation_id = generation_id
        sources = tuple(
            sorted(
                sources,
                key=lambda item: (item.source_type.value, item.source_id_hash),
            )
        )
        chunks = tuple(
            sorted(
                chunks,
                key=lambda item: (
                    item.source_id,
                    item.source_version,
                    item.ordinal,
                    item.chunk_id,
                ),
            )
        )
        evidence_index = ProjectEvidenceIndex(context, sources, chunks)
        return ProjectRuntimeSnapshot(
            project_id=envelope.project_id,
            generation_id=runtime_generation_id,
            context=context,
            source_states=runtime_states,
            sources=sources,
            chunks=chunks,
            evidence_index=evidence_index,
        )

    def _decrypt_generation(
        self,
        active: StoredProjectGeneration | None,
        keys: set[tuple[SyncSourceType, str]] | None,
    ) -> tuple[tuple[EvidenceSource, ...], tuple[EvidenceChunk, ...]]:
        if active is None:
            return (), ()
        sources: list[EvidenceSource] = []
        source_ids: dict[tuple[SyncSourceType, str], str] = {}
        for item in active.protected_sources:
            key = (item.source_type, item.source_id_hash)
            if keys is not None and key not in keys:
                continue
            try:
                source_id = self._unprotect_text(
                    active.project_id,
                    item.protected_source_id,
                )
                source_title = self._unprotect_text(
                    active.project_id,
                    item.protected_title,
                )
                source_url = self._unprotect_text(
                    active.project_id,
                    item.protected_url,
                )
                if (
                    item.source_version is None
                    or item.source_time is None
                    or item.content_hash is None
                ):
                    raise ValueError("incomplete protected source")
                source = EvidenceSource(
                    source_type=item.source_type,
                    source_id=source_id,
                    source_id_hash=item.source_id_hash,
                    source_title=source_title,
                    source_url=source_url,
                    source_version=item.source_version,
                    source_time=item.source_time,
                    permission_hash=item.permission_hash,
                    content_hash=item.content_hash,
                )
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                raise ProjectSyncValidationError(
                    "protected_content_invalid"
                ) from None
            sources.append(source)
            source_ids[key] = source_id

        chunks: list[EvidenceChunk] = []
        for item in active.protected_chunks:
            key = (item.source_type, item.source_id_hash)
            if keys is not None and key not in keys:
                continue
            source_id = source_ids.get(key)
            if source_id is None:
                raise ProjectSyncValidationError("protected_content_invalid")
            try:
                heading_value = json.loads(
                    self._unprotect_text(
                        active.project_id,
                        item.protected_heading_path,
                    )
                )
                if not isinstance(heading_value, list) or any(
                    not isinstance(value, str) for value in heading_value
                ):
                    raise ValueError("invalid heading path")
                text = self._unprotect_text(
                    active.project_id,
                    item.protected_text,
                )
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != (
                    item.content_hash
                ):
                    raise ValueError("invalid text hash")
                chunk = EvidenceChunk(
                    chunk_id=item.chunk_id,
                    source_id=source_id,
                    source_version=item.source_version,
                    ordinal=item.ordinal,
                    heading_path=tuple(heading_value),
                    text=text,
                    start_offset=item.start_offset,
                    end_offset=item.end_offset,
                    content_hash=item.content_hash,
                )
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                raise ProjectSyncValidationError(
                    "protected_content_invalid"
                ) from None
            chunks.append(chunk)
        return tuple(sources), tuple(chunks)

    def _unprotect_text(self, project_id: str, protected: bytes) -> str:
        plaintext = self._protector.unprotect(project_id, protected)
        if not plaintext:
            raise ProjectSyncValidationError("protected_content_invalid")
        return plaintext.decode("utf-8")

    @staticmethod
    def _unchanged_runtime_states(
        envelope: SyncEnvelope,
        active: StoredProjectGeneration,
        incoming: tuple[SourceState, ...],
    ) -> tuple[SourceState, ...]:
        if envelope.source_cursor <= active.source_cursor:
            return active.source_states
        incoming_by_key = {
            (item.source_type, item.source_id_hash): item for item in incoming
        }
        renewed: list[SourceState] = []
        for state in active.source_states:
            candidate = incoming_by_key.get(
                (state.source_type, state.source_id_hash)
            )
            if candidate is None or candidate.status is not SourceSyncStatus.ACTIVE:
                renewed.append(state)
                continue
            renewed.append(
                SourceState(
                    project_id=state.project_id,
                    source_type=state.source_type,
                    source_id_hash=state.source_id_hash,
                    source_version=state.source_version,
                    content_hash=state.content_hash,
                    permission_hash=state.permission_hash,
                    status=state.status,
                    last_attempt_at=candidate.last_attempt_at,
                    last_success_at=candidate.last_success_at,
                    last_error_type=state.last_error_type,
                )
            )
        return tuple(renewed)

    @staticmethod
    def _expected_outcome(
        envelope: SyncEnvelope,
        active: StoredProjectGeneration | None,
        states: tuple[SourceState, ...],
        generation_id: str,
    ) -> ProjectSyncOutcome:
        if active is not None and envelope.content_hash == active.content_hash:
            if envelope.source_cursor > active.source_cursor:
                return ProjectSyncOutcome.UNCHANGED
            if (
                envelope.source_cursor == active.source_cursor
                and generation_id != active.generation_id
            ):
                return ProjectSyncOutcome.UNCHANGED
        if any(
            item.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}
            for item in states
        ):
            return ProjectSyncOutcome.DEGRADED
        return ProjectSyncOutcome.APPLIED

    def _project_state(self, state: SourceState, now: datetime) -> SourceState:
        if state.status is not SourceSyncStatus.ACTIVE or not self._is_expired(
            state,
            now,
        ):
            return state.model_copy()
        return state.model_copy(
            update={
                "status": SourceSyncStatus.STALE,
                "last_error_type": SourceErrorType.UNKNOWN,
            }
        )

    def _is_fresh(self, state: SourceState, now: datetime) -> bool:
        return (
            state.status
            not in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED}
            and state.last_success_at is not None
            and not self._is_expired(state, now)
        )

    def _is_expired(self, state: SourceState, now: datetime) -> bool:
        return (
            state.last_success_at is not None
            and (now - state.last_success_at).total_seconds()
            > self._source_freshness_seconds
        )

    def _read_monotonic(self) -> float:
        return self._validate_monotonic(self._monotonic())

    @staticmethod
    def _validate_monotonic(value: float) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ProjectSyncValidationError("monotonic_invalid")
        return float(value)

    def _reset_clock(self, wall_now: datetime, monotonic_now: float) -> None:
        with self._clock_lock:
            self._last_wall = wall_now
            self._last_monotonic = monotonic_now
            self._clock_untrusted = False
            self._immediate_sync_required = False


__all__ = [
    "ClockCheckResult",
    "ProjectSourceUnavailable",
    "ProjectSyncError",
    "ProjectSyncService",
    "ProjectSyncValidationError",
    "compute_envelope_content_hash",
]
