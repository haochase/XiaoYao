from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from companion_gateway.project.models import ProjectContextPackage
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.sync_models import (
    RetrievalRequest,
    RetrievalRequestStatus,
    RetrievalSourceBaseline,
    SourceState,
    SourceSyncStatus,
    SyncAudit,
    SyncEnvelope,
    SyncSourceType,
)


class SyncConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtectedSourceRecord:
    source_type: SyncSourceType
    source_id_hash: str
    protected_source_id: bytes
    protected_title: bytes
    protected_url: bytes
    source_version: str | None
    source_time: datetime | None
    permission_hash: str
    content_hash: str | None


@dataclass(frozen=True)
class ProtectedChunkRecord:
    chunk_id: str
    source_type: SyncSourceType
    source_id_hash: str
    source_version: str
    ordinal: int
    protected_heading_path: bytes
    protected_text: bytes
    start_offset: int
    end_offset: int
    content_hash: str


@dataclass(frozen=True)
class SyncCommit:
    envelope: SyncEnvelope
    generation_id: str
    source_states: tuple[SourceState, ...]
    protected_sources: tuple[ProtectedSourceRecord, ...]
    protected_chunks: tuple[ProtectedChunkRecord, ...]
    audit: SyncAudit


@dataclass(frozen=True)
class SyncCommitResult:
    outcome: Literal["applied", "unchanged", "degraded"]
    generation_id: str
    source_cursor: int
    completed_retrieval_request_ids: tuple[str, ...]


@dataclass(frozen=True)
class StoredProjectGeneration:
    project_id: str
    generation_id: str
    sync_id: str
    source_cursor: int
    content_hash: str
    context: ProjectContextPackage
    source_states: tuple[SourceState, ...]
    protected_sources: tuple[ProtectedSourceRecord, ...]
    protected_chunks: tuple[ProtectedChunkRecord, ...]


@dataclass(frozen=True)
class SharedClockState:
    trusted_wall_at: datetime | None
    last_observed_wall_at: datetime | None
    clock_untrusted: bool
    needs_sync: bool
    reason: Literal["normal", "resume_detected", "clock_rollback"]


class ProjectSyncRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._configured_protection: tuple[str, str] | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            ProjectMemoryRepository._initialize_tables(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_sync_generations (
                    project_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    sync_id TEXT NOT NULL,
                    source_cursor INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, generation_id),
                    UNIQUE (project_id, source_cursor),
                    UNIQUE (sync_id)
                );

                CREATE TABLE IF NOT EXISTS project_active_generations (
                    project_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_protection_metadata (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    identity_digest TEXT NOT NULL,
                    protector_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_sync_clock_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    trusted_wall_at TEXT,
                    last_observed_wall_at TEXT,
                    clock_untrusted INTEGER NOT NULL,
                    needs_sync INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_source_states (
                    project_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id_hash TEXT NOT NULL,
                    source_version TEXT,
                    source_time TEXT,
                    content_hash TEXT,
                    permission_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_error_type TEXT,
                    protected_source_id BLOB,
                    protected_title BLOB,
                    protected_url BLOB,
                    PRIMARY KEY (
                        project_id,
                        generation_id,
                        source_type,
                        source_id_hash
                    )
                );

                CREATE TABLE IF NOT EXISTS project_source_heads (
                    project_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id_hash TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    source_time TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (project_id, source_type, source_id_hash)
                );

                CREATE TABLE IF NOT EXISTS project_evidence_chunks (
                    project_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id_hash TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    protected_heading_path BLOB NOT NULL,
                    protected_text BLOB NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (project_id, generation_id, chunk_id)
                );

                CREATE TABLE IF NOT EXISTS project_sync_audits (
                    sync_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    source_counts_json TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_type TEXT
                );

                CREATE TABLE IF NOT EXISTS project_retrieval_requests (
                    request_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    source_id_hashes_json TEXT NOT NULL,
                    baseline_generation_id TEXT,
                    baseline_content_hash TEXT,
                    baseline_source_cursor INTEGER,
                    baseline_sources_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_project_sync_audits_project_finished
                ON project_sync_audits(project_id, finished_at);

                CREATE INDEX IF NOT EXISTS idx_project_retrieval_status_created
                ON project_retrieval_requests(project_id, status, created_at);

                INSERT OR IGNORE INTO project_sync_clock_state(
                    singleton_id, trusted_wall_at, clock_untrusted,
                    needs_sync, reason
                ) VALUES (1, NULL, 0, 0, 'normal');
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO project_source_heads(
                    project_id, source_type, source_id_hash, source_version,
                    source_time, content_hash
                )
                SELECT state.project_id, state.source_type,
                       state.source_id_hash, state.source_version,
                       state.source_time, state.content_hash
                FROM project_source_states AS state
                JOIN project_active_generations AS active
                  ON active.project_id = state.project_id
                 AND active.generation_id = state.generation_id
                WHERE state.status = 'active'
                  AND state.source_version IS NOT NULL
                  AND state.source_time IS NOT NULL
                  AND state.content_hash IS NOT NULL
                """
            )
            self._ensure_column(
                connection,
                "project_sync_clock_state",
                "last_observed_wall_at",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "lease_expires_at",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                UPDATE project_retrieval_requests
                SET status = 'pending'
                WHERE status = 'in_progress'
                  AND lease_expires_at IS NULL
                """
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "baseline_generation_id",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "baseline_content_hash",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "baseline_source_cursor",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "project_retrieval_requests",
                "baseline_sources_json",
                "TEXT",
            )
            connection.execute(
                """
                UPDATE project_retrieval_requests
                SET status = CASE
                        WHEN status IN ('pending', 'in_progress')
                        THEN 'expired'
                        ELSE status
                    END,
                    baseline_generation_id = NULL,
                    baseline_content_hash = NULL,
                    baseline_source_cursor = NULL,
                    lease_expires_at = NULL
                WHERE baseline_generation_id IS NULL
                   OR baseline_content_hash IS NULL
                   OR baseline_source_cursor IS NULL
                   OR baseline_sources_json IS NULL
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def configure_protection(
        self,
        identity_digest: str,
        protector_version: str,
    ) -> None:
        self._validate_protection_descriptor(identity_digest, protector_version)
        descriptor = (identity_digest, protector_version)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT identity_digest, protector_version
                FROM project_protection_metadata
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                active = connection.execute(
                    "SELECT 1 FROM project_active_generations LIMIT 1"
                ).fetchone()
                if active is not None:
                    raise SyncConflict("protection_metadata_missing")
                connection.execute(
                    """
                    INSERT INTO project_protection_metadata(
                        singleton_id, identity_digest, protector_version
                    ) VALUES (1, ?, ?)
                    """,
                    descriptor,
                )
            else:
                stored = (
                    str(row["identity_digest"]),
                    str(row["protector_version"]),
                )
                if stored[0] != identity_digest:
                    raise SyncConflict("protection_identity_mismatch")
                if stored[1] != protector_version:
                    raise SyncConflict("protection_version_mismatch")
        self._configured_protection = descriptor

    def adopt_verified_protection(
        self,
        identity_digest: str,
        protector_version: str,
        verified_generations: tuple[StoredProjectGeneration, ...],
    ) -> None:
        self._validate_protection_descriptor(identity_digest, protector_version)
        descriptor = (identity_digest, protector_version)
        expected = tuple(
            sorted(
                (
                    item.project_id,
                    item.generation_id,
                    item.source_cursor,
                    item.content_hash,
                )
                for item in verified_generations
            )
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT identity_digest, protector_version
                FROM project_protection_metadata
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is not None:
                stored = (
                    str(row["identity_digest"]),
                    str(row["protector_version"]),
                )
                if stored[0] != identity_digest:
                    raise SyncConflict("protection_identity_mismatch")
                if stored[1] != protector_version:
                    raise SyncConflict("protection_version_mismatch")
            else:
                actual = tuple(
                    (
                        str(item["project_id"]),
                        str(item["generation_id"]),
                        int(item["source_cursor"]),
                        str(item["content_hash"]),
                    )
                    for item in connection.execute(
                        """
                        SELECT generation.project_id,
                               generation.generation_id,
                               generation.source_cursor,
                               generation.content_hash
                        FROM project_active_generations AS active
                        JOIN project_sync_generations AS generation
                          ON generation.project_id = active.project_id
                         AND generation.generation_id = active.generation_id
                        ORDER BY generation.project_id
                        """
                    )
                )
                if actual != expected:
                    raise SyncConflict("protection_adoption_conflict")
                connection.execute(
                    """
                    INSERT INTO project_protection_metadata(
                        singleton_id, identity_digest, protector_version
                    ) VALUES (1, ?, ?)
                    """,
                    descriptor,
                )
        self._configured_protection = descriptor

    @staticmethod
    def _validate_protection_descriptor(
        identity_digest: str,
        protector_version: str,
    ) -> None:
        if (
            len(identity_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in identity_digest
            )
        ):
            raise ValueError("protection_identity_invalid")
        if (
            not protector_version.strip()
            or len(protector_version) > 128
            or any(character.isspace() for character in protector_version)
        ):
            raise ValueError("protection_version_invalid")

    def protection_descriptor(self) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT identity_digest, protector_version
                FROM project_protection_metadata
                WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return str(row["identity_digest"]), str(row["protector_version"])

    def has_active_generation(self, project_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM project_active_generations
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        return row is not None

    def load_clock_state(self) -> SharedClockState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trusted_wall_at, last_observed_wall_at,
                       clock_untrusted, needs_sync, reason
                FROM project_sync_clock_state
                WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("clock_state_missing")
        return _clock_state_from_row(row)

    def observe_wall_clock(
        self,
        wall_now: datetime,
        *,
        rollback_threshold_seconds: float,
    ) -> SharedClockState:
        if wall_now.tzinfo is None or wall_now.utcoffset() is None:
            raise ValueError("wall_now_must_be_aware")
        if rollback_threshold_seconds < 0:
            raise ValueError("clock_rollback_threshold_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT trusted_wall_at, last_observed_wall_at,
                       clock_untrusted, needs_sync, reason
                FROM project_sync_clock_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("clock_state_missing")
            current = _clock_state_from_row(row)
            baseline = (
                current.last_observed_wall_at or current.trusted_wall_at
            )
            rollback = (
                baseline is not None
                and (baseline - wall_now).total_seconds()
                > rollback_threshold_seconds
            )
            observed = current.last_observed_wall_at
            if observed is None or wall_now > observed:
                observed = wall_now
            clock_untrusted = current.clock_untrusted or rollback
            needs_sync = current.needs_sync or rollback
            reason = "clock_rollback" if clock_untrusted else current.reason
            connection.execute(
                """
                UPDATE project_sync_clock_state
                SET last_observed_wall_at = ?, clock_untrusted = ?,
                    needs_sync = ?, reason = ?
                WHERE singleton_id = 1
                """,
                (
                    _datetime_text(observed),
                    int(clock_untrusted),
                    int(needs_sync),
                    reason,
                ),
            )
        return SharedClockState(
            trusted_wall_at=current.trusted_wall_at,
            last_observed_wall_at=observed,
            clock_untrusted=clock_untrusted,
            needs_sync=needs_sync,
            reason=reason,
        )

    def mark_clock_state(
        self,
        *,
        clock_untrusted: bool,
        needs_sync: bool,
        reason: Literal["resume_detected", "clock_rollback"],
    ) -> SharedClockState:
        if not clock_untrusted and not needs_sync:
            raise ValueError("clock_state_change_empty")
        if clock_untrusted and reason != "clock_rollback":
            raise ValueError("clock_state_reason_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT trusted_wall_at, last_observed_wall_at,
                       clock_untrusted, needs_sync, reason
                FROM project_sync_clock_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("clock_state_missing")
            current = _clock_state_from_row(row)
            updated_untrusted = current.clock_untrusted or clock_untrusted
            updated_needs_sync = current.needs_sync or needs_sync
            updated_reason = (
                "clock_rollback"
                if updated_untrusted
                else "resume_detected"
            )
            connection.execute(
                """
                UPDATE project_sync_clock_state
                SET clock_untrusted = ?, needs_sync = ?, reason = ?
                WHERE singleton_id = 1
                """,
                (
                    int(updated_untrusted),
                    int(updated_needs_sync),
                    updated_reason,
                ),
            )
        return SharedClockState(
            trusted_wall_at=current.trusted_wall_at,
            last_observed_wall_at=current.last_observed_wall_at,
            clock_untrusted=updated_untrusted,
            needs_sync=updated_needs_sync,
            reason=updated_reason,
        )

    def list_active_project_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            self._assert_protection_access(connection)
            rows = connection.execute(
                """
                SELECT project_id FROM project_active_generations
                ORDER BY project_id
                """
            ).fetchall()
        return tuple(str(row["project_id"]) for row in rows)

    def commit(self, candidate: SyncCommit) -> SyncCommitResult:
        self._validate_candidate(candidate)
        envelope = candidate.envelope
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_protection_access(connection)
            active = self._load_active_row(connection, envelope.project_id)
            stored_context = self._load_context(connection, envelope.project_id)
            if active is not None:
                active_cursor = int(active["source_cursor"])
                active_hash = str(active["content_hash"])
                if envelope.source_cursor < active_cursor:
                    raise SyncConflict("stale_cursor")
                self._validate_source_heads(connection, candidate)
                self._check_context_conflicts(stored_context, envelope.context)
                if envelope.source_cursor == active_cursor:
                    if envelope.content_hash != active_hash:
                        raise SyncConflict("cursor_content_conflict")
                    stored_result = self._stored_result(
                        connection,
                        candidate,
                        active,
                    )
                    self._validate_audit(candidate, stored_result.outcome)
                    return stored_result
                if envelope.content_hash == active_hash:
                    self._validate_audit(candidate, "unchanged")
                    return self._commit_unchanged(connection, candidate, active)

            if active is None:
                self._check_context_conflicts(stored_context, envelope.context)
                self._validate_source_heads(connection, candidate)
            outcome: Literal["applied", "degraded"] = (
                "degraded"
                if any(
                    item.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}
                    for item in candidate.source_states
                )
                else "applied"
            )
            self._validate_audit(candidate, outcome)
            prior_generation_id = (
                str(active["generation_id"]) if active is not None else None
            )
            effective_states, effective_sources, effective_chunks = (
                self._build_effective_records(
                    connection,
                    candidate,
                    prior_generation_id,
                )
            )
            self._insert_source_states(
                connection,
                envelope.project_id,
                candidate.generation_id,
                effective_states,
                effective_sources,
            )
            self._insert_chunks(
                connection,
                envelope.project_id,
                candidate.generation_id,
                effective_chunks,
            )
            self._upsert_source_heads(connection, candidate)
            completed = self._complete_retrieval_requests(
                connection,
                candidate,
            )
            connection.execute(
                """
                INSERT INTO project_contexts(project_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (envelope.project_id, envelope.context.model_dump_json()),
            )
            connection.execute(
                """
                INSERT INTO project_sync_generations(
                    project_id, generation_id, sync_id, source_cursor,
                    content_hash, context_json, outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.project_id,
                    candidate.generation_id,
                    envelope.sync_id,
                    envelope.source_cursor,
                    envelope.content_hash,
                    _canonical_json(envelope.context.model_dump(mode="json")),
                    outcome,
                    _datetime_text(candidate.audit.finished_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO project_active_generations(project_id, generation_id)
                VALUES (?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    generation_id = excluded.generation_id
                """,
                (envelope.project_id, candidate.generation_id),
            )
            self._insert_audit(connection, candidate.audit, outcome)
            self._record_successful_sync(connection, candidate.audit.finished_at)
            connection.execute(
                """
                DELETE FROM project_evidence_chunks
                WHERE project_id = ? AND generation_id <> ?
                """,
                (envelope.project_id, candidate.generation_id),
            )
            connection.execute(
                """
                DELETE FROM project_source_states
                WHERE project_id = ? AND generation_id <> ?
                """,
                (envelope.project_id, candidate.generation_id),
            )
        return SyncCommitResult(
            outcome=outcome,
            generation_id=candidate.generation_id,
            source_cursor=envelope.source_cursor,
            completed_retrieval_request_ids=completed,
        )

    def load_active_generation(
        self,
        project_id: str,
    ) -> StoredProjectGeneration | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            self._assert_protection_access(connection)
            generation = self._load_active_row(connection, project_id)
            if generation is None:
                return None
            state_rows = connection.execute(
                """
                SELECT * FROM project_source_states
                WHERE project_id = ? AND generation_id = ?
                ORDER BY source_type, source_id_hash
                """,
                (project_id, generation["generation_id"]),
            ).fetchall()
            chunk_rows = connection.execute(
                """
                SELECT * FROM project_evidence_chunks
                WHERE project_id = ? AND generation_id = ?
                ORDER BY source_type, source_id_hash, ordinal, chunk_id
                """,
                (project_id, generation["generation_id"]),
            ).fetchall()
        return StoredProjectGeneration(
            project_id=project_id,
            generation_id=str(generation["generation_id"]),
            sync_id=str(generation["sync_id"]),
            source_cursor=int(generation["source_cursor"]),
            content_hash=str(generation["content_hash"]),
            context=ProjectContextPackage.model_validate_json(
                generation["context_json"]
            ),
            source_states=tuple(_state_from_row(row) for row in state_rows),
            protected_sources=tuple(
                _protected_source_from_row(row)
                for row in state_rows
                if row["protected_source_id"] is not None
            ),
            protected_chunks=tuple(
                _protected_chunk_from_row(row) for row in chunk_rows
            ),
        )

    def _assert_protection_access(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT identity_digest, protector_version
            FROM project_protection_metadata
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            return
        if self._configured_protection is None:
            raise SyncConflict("protection_identity_required")
        stored = (
            str(row["identity_digest"]),
            str(row["protector_version"]),
        )
        if stored[0] != self._configured_protection[0]:
            raise SyncConflict("protection_identity_mismatch")
        if stored[1] != self._configured_protection[1]:
            raise SyncConflict("protection_version_mismatch")

    def save_retrieval_request(self, request: RetrievalRequest) -> RetrievalRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._load_active_row(connection, request.project_id)
            row = connection.execute(
                """
                SELECT * FROM project_retrieval_requests
                WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if row is not None:
                stored = _retrieval_from_row(row)
                if (
                    stored.project_id != request.project_id
                    or stored.query_hash != request.query_hash
                    or stored.source_id_hashes != request.source_id_hashes
                    or request.status is not RetrievalRequestStatus.PENDING
                    or request.completed_at is not None
                ):
                    raise SyncConflict("retrieval_request_conflict")
                return stored
            if request.status is not RetrievalRequestStatus.PENDING:
                raise ValueError("new retrieval request must be pending")
            if active is None:
                raise SyncConflict("retrieval_source_unavailable")
            baseline = (
                str(active["generation_id"]),
                str(active["content_hash"]),
                int(active["source_cursor"]),
            )
            baseline_sources = self._source_baselines_for_generation(
                connection,
                request.project_id,
                baseline[0],
                request.source_id_hashes,
            )
            supplied = (
                request.baseline_generation_id,
                request.baseline_content_hash,
                request.baseline_source_cursor,
            )
            if any(item is not None for item in supplied) and (
                supplied != baseline
                or request.baseline_sources != baseline_sources
            ):
                raise SyncConflict("retrieval_request_conflict")
            request = request.model_copy(
                update={
                    "baseline_generation_id": baseline[0],
                    "baseline_content_hash": baseline[1],
                    "baseline_source_cursor": baseline[2],
                    "baseline_sources": baseline_sources,
                }
            )
            connection.execute(
                """
                INSERT INTO project_retrieval_requests(
                    request_id, project_id, query_hash, source_id_hashes_json,
                    baseline_generation_id, baseline_content_hash,
                    baseline_source_cursor, baseline_sources_json, status,
                    created_at, expires_at, lease_expires_at, attempt_count,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _retrieval_values(request),
            )
        return request

    def get_retrieval_request(
        self,
        project_id: str,
        request_id: str,
    ) -> RetrievalRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_retrieval_requests
                WHERE project_id = ? AND request_id = ?
                """,
                (project_id, request_id),
            ).fetchone()
        return _retrieval_from_row(row) if row is not None else None

    def list_retrieval_requests(
        self,
        project_id: str,
        status: RetrievalRequestStatus | None = None,
    ) -> tuple[RetrievalRequest, ...]:
        query = """
            SELECT * FROM project_retrieval_requests
            WHERE project_id = ?
        """
        parameters: tuple[object, ...] = (project_id,)
        if status is not None:
            query += " AND status = ?"
            parameters += (status.value,)
        query += " ORDER BY created_at, request_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_retrieval_from_row(row) for row in rows)

    def claim_retrieval_requests(
        self,
        project_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> tuple[RetrievalRequest, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("retrieval_claim_now_invalid")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 1_800
        ):
            raise ValueError("retrieval_lease_invalid")
        now_text = _datetime_text(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE project_retrieval_requests
                SET status = ?, lease_expires_at = NULL
                WHERE project_id = ?
                  AND status IN (?, ?)
                  AND expires_at <= ?
                """,
                (
                    RetrievalRequestStatus.EXPIRED.value,
                    project_id,
                    RetrievalRequestStatus.PENDING.value,
                    RetrievalRequestStatus.IN_PROGRESS.value,
                    now_text,
                ),
            )
            rows = connection.execute(
                """
                SELECT * FROM project_retrieval_requests
                WHERE project_id = ? AND expires_at > ?
                  AND (
                    status = ?
                    OR (
                      status = ?
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    )
                  )
                ORDER BY created_at, request_id
                LIMIT 100
                """,
                (
                    project_id,
                    now_text,
                    RetrievalRequestStatus.PENDING.value,
                    RetrievalRequestStatus.IN_PROGRESS.value,
                    now_text,
                ),
            ).fetchall()
            claimed: list[RetrievalRequest] = []
            for row in rows:
                expires_at = _parse_datetime(row["expires_at"])
                lease_expires_at = min(
                    now + timedelta(seconds=lease_seconds),
                    expires_at,
                )
                connection.execute(
                    """
                    UPDATE project_retrieval_requests
                    SET status = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE request_id = ? AND project_id = ?
                    """,
                    (
                        RetrievalRequestStatus.IN_PROGRESS.value,
                        _datetime_text(lease_expires_at),
                        row["request_id"],
                        project_id,
                    ),
                )
                updated = connection.execute(
                    """
                    SELECT * FROM project_retrieval_requests
                    WHERE request_id = ? AND project_id = ?
                    """,
                    (row["request_id"], project_id),
                ).fetchone()
                assert updated is not None
                claimed.append(_retrieval_from_row(updated))
        return tuple(claimed)

    def compare_and_set_retrieval_request(
        self,
        project_id: str,
        request_id: str,
        expected: frozenset[RetrievalRequestStatus],
        target: RetrievalRequestStatus,
        completed_at: datetime | None = None,
    ) -> bool:
        if target is RetrievalRequestStatus.COMPLETED:
            raise ValueError("completed transition requires sync commit")
        if target is not RetrievalRequestStatus.EXPIRED:
            raise ValueError("unsupported retrieval request transition")
        if completed_at is not None:
            raise ValueError("public retrieval transition forbids completed_at")
        if not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        parameters = (
            target.value,
            project_id,
            request_id,
            *(item.value for item in expected),
        )
        with self._connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE project_retrieval_requests
                SET status = ?, completed_at = NULL, lease_expires_at = NULL
                WHERE project_id = ? AND request_id = ?
                AND status IN ({placeholders})
                """,
                parameters,
            )
        return updated.rowcount == 1

    @staticmethod
    def _load_active_row(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT generation.*
            FROM project_active_generations AS active
            JOIN project_sync_generations AS generation
              ON generation.project_id = active.project_id
             AND generation.generation_id = active.generation_id
            WHERE active.project_id = ?
            """,
            (project_id,),
        ).fetchone()

    @staticmethod
    def _load_context(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> ProjectContextPackage | None:
        row = connection.execute(
            "SELECT payload_json FROM project_contexts WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return (
            ProjectContextPackage.model_validate_json(row["payload_json"])
            if row is not None
            else None
        )

    @staticmethod
    def _check_context_conflicts(
        stored: ProjectContextPackage | None,
        candidate: ProjectContextPackage,
    ) -> None:
        if stored is None:
            return
        if stored.permission_scope != candidate.permission_scope:
            raise SyncConflict("permission_conflict")
        if stored.active_decisions != candidate.active_decisions:
            raise SyncConflict("decision_change_requires_review")

    @staticmethod
    def _validate_source_heads(
        connection: sqlite3.Connection,
        candidate: SyncCommit,
    ) -> None:
        for source in candidate.envelope.sources:
            if source.status is not SourceSyncStatus.ACTIVE:
                continue
            assert source.source_version is not None
            assert source.source_time is not None
            assert source.content_hash is not None
            source_hash = hashlib.sha256(
                source.source_id.encode("utf-8")
            ).hexdigest()
            row = connection.execute(
                """
                SELECT source_version, source_time, content_hash
                FROM project_source_heads
                WHERE project_id = ? AND source_type = ?
                  AND source_id_hash = ?
                """,
                (
                    candidate.envelope.project_id,
                    source.source_type.value,
                    source_hash,
                ),
            ).fetchone()
            if row is None:
                continue
            previous_time = _parse_datetime(str(row["source_time"]))
            if source.source_time < previous_time:
                raise SyncConflict("source_version_rollback")
            if source.source_version == row["source_version"] and (
                source.content_hash != row["content_hash"]
                or source.source_time != previous_time
            ):
                raise SyncConflict("source_version_conflict")

    @staticmethod
    def _upsert_source_heads(
        connection: sqlite3.Connection,
        candidate: SyncCommit,
    ) -> None:
        for source in candidate.envelope.sources:
            if source.status is not SourceSyncStatus.ACTIVE:
                continue
            assert source.source_version is not None
            assert source.source_time is not None
            assert source.content_hash is not None
            connection.execute(
                """
                INSERT INTO project_source_heads(
                    project_id, source_type, source_id_hash, source_version,
                    source_time, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source_type, source_id_hash)
                DO UPDATE SET source_version = excluded.source_version,
                              source_time = excluded.source_time,
                              content_hash = excluded.content_hash
                """,
                (
                    candidate.envelope.project_id,
                    source.source_type.value,
                    hashlib.sha256(source.source_id.encode("utf-8")).hexdigest(),
                    source.source_version,
                    _datetime_text(source.source_time),
                    source.content_hash,
                ),
            )

    @staticmethod
    def _validate_candidate(candidate: SyncCommit) -> None:
        envelope = candidate.envelope
        if not candidate.generation_id.strip():
            raise ValueError("generation_id_invalid")
        if candidate.audit.project_id != envelope.project_id:
            raise ValueError("audit_project_mismatch")
        if candidate.audit.sync_id != envelope.sync_id:
            raise ValueError("audit_sync_mismatch")
        state_keys = [
            (item.source_type, item.source_id_hash) for item in candidate.source_states
        ]
        if any(
            item.project_id != envelope.project_id
            for item in candidate.source_states
        ):
            raise ValueError("source_state_project_mismatch")
        if len(state_keys) != len(set(state_keys)):
            raise ValueError("duplicate_source_state")
        state_by_key = {
            (item.source_type, item.source_id_hash): item
            for item in candidate.source_states
        }
        snapshots_by_key = {
            (
                item.source_type,
                hashlib.sha256(item.source_id.encode("utf-8")).hexdigest(),
            ): item
            for item in envelope.sources
        }
        tombstones_by_key = {
            (
                item.source_type,
                hashlib.sha256(item.source_id.encode("utf-8")).hexdigest(),
            ): item
            for item in envelope.tombstones
        }
        if set(state_keys) != set(snapshots_by_key) | set(tombstones_by_key):
            raise ValueError("source_state_envelope_mismatch")
        for key, state in state_by_key.items():
            envelope_record = snapshots_by_key.get(key) or tombstones_by_key.get(key)
            if envelope_record is None or state.status is not envelope_record.status:
                raise ValueError("source_state_envelope_mismatch")
            snapshot = snapshots_by_key.get(key)
            if snapshot is None:
                continue
            if state.permission_hash != snapshot.permission_hash:
                raise SyncConflict("source_snapshot_conflict")
            if state.status is SourceSyncStatus.STALE and (
                state.source_version != snapshot.source_version
                or state.content_hash != snapshot.content_hash
            ):
                raise SyncConflict("source_snapshot_conflict")
        source_keys = [
            (item.source_type, item.source_id_hash)
            for item in candidate.protected_sources
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("duplicate_protected_source")
        if any(key not in set(state_keys) for key in source_keys):
            raise ValueError("protected_source_state_missing")
        for source in candidate.protected_sources:
            key = (source.source_type, source.source_id_hash)
            state = state_by_key[key]
            snapshot = snapshots_by_key.get(key)
            if (
                snapshot is None
                or source.source_version != state.source_version
                or source.source_version != snapshot.source_version
                or source.source_time != snapshot.source_time
                or source.permission_hash != state.permission_hash
                or source.permission_hash != snapshot.permission_hash
                or source.content_hash != state.content_hash
                or source.content_hash != snapshot.content_hash
                or not all(
                    isinstance(value, bytes) and value
                    for value in (
                        source.protected_source_id,
                        source.protected_title,
                        source.protected_url,
                    )
                )
            ):
                raise ValueError("protected_source_mismatch")
        if len({item.chunk_id for item in candidate.protected_chunks}) != len(
            candidate.protected_chunks
        ):
            raise ValueError("duplicate_protected_chunk")
        if any(
            (item.source_type, item.source_id_hash) not in set(state_keys)
            for item in candidate.protected_chunks
        ):
            raise ValueError("protected_chunk_state_missing")
        expected_chunk_keys = {
            (source_type, source_id_hash, chunk.chunk_id)
            for (source_type, source_id_hash), snapshot in snapshots_by_key.items()
            if snapshot.status is SourceSyncStatus.ACTIVE
            for chunk in snapshot.chunks
        }
        protected_chunk_keys = {
            (item.source_type, item.source_id_hash, item.chunk_id)
            for item in candidate.protected_chunks
        }
        if expected_chunk_keys - protected_chunk_keys:
            raise ValueError("protected_chunk_missing")
        for chunk in candidate.protected_chunks:
            key = (chunk.source_type, chunk.source_id_hash)
            state = state_by_key[key]
            snapshot = snapshots_by_key.get(key)
            snapshot_chunk = (
                next(
                    (
                        item
                        for item in snapshot.chunks
                        if item.chunk_id == chunk.chunk_id
                    ),
                    None,
                )
                if snapshot is not None
                else None
            )
            if (
                state.status is not SourceSyncStatus.ACTIVE
                or snapshot_chunk is None
                or chunk.source_version != state.source_version
                or chunk.source_version != snapshot_chunk.source_version
                or chunk.ordinal != snapshot_chunk.ordinal
                or chunk.start_offset != snapshot_chunk.start_offset
                or chunk.end_offset != snapshot_chunk.end_offset
                or chunk.content_hash != snapshot_chunk.content_hash
                or not isinstance(chunk.protected_heading_path, bytes)
                or not chunk.protected_heading_path
                or not isinstance(chunk.protected_text, bytes)
                or not chunk.protected_text
            ):
                raise ValueError("protected_chunk_mismatch")

    @staticmethod
    def _validate_audit(
        candidate: SyncCommit,
        outcome: Literal["applied", "unchanged", "degraded"],
    ) -> None:
        expected_counts = dict(Counter(item.status for item in candidate.source_states))
        if (
            candidate.audit.source_counts_by_status != expected_counts
            or candidate.audit.chunk_count != len(candidate.protected_chunks)
            or candidate.audit.outcome != outcome
        ):
            raise SyncConflict("audit_mismatch")

    def _stored_result(
        self,
        connection: sqlite3.Connection,
        candidate: SyncCommit,
        active: sqlite3.Row,
    ) -> SyncCommitResult:
        completed = self._already_completed_request_ids(connection, candidate)
        audit = connection.execute(
            """
            SELECT outcome FROM project_sync_audits
            WHERE sync_id = ? AND project_id = ?
            """,
            (candidate.audit.sync_id, candidate.audit.project_id),
        ).fetchone()
        return SyncCommitResult(
            outcome=str(audit["outcome"] if audit is not None else active["outcome"]),
            generation_id=str(active["generation_id"]),
            source_cursor=int(active["source_cursor"]),
            completed_retrieval_request_ids=completed,
        )

    def _commit_unchanged(
        self,
        connection: sqlite3.Connection,
        candidate: SyncCommit,
        active: sqlite3.Row,
    ) -> SyncCommitResult:
        project_id = candidate.envelope.project_id
        generation_id = str(active["generation_id"])
        for state in candidate.source_states:
            updated = connection.execute(
                """
                UPDATE project_source_states
                SET last_attempt_at = ?,
                    last_success_at = CASE WHEN ? = 'active' THEN ?
                                           ELSE last_success_at END,
                    last_error_type = ?
                WHERE project_id = ? AND generation_id = ?
                  AND source_type = ? AND source_id_hash = ?
                """,
                (
                    _datetime_text(state.last_attempt_at),
                    state.status.value,
                    _optional_datetime_text(state.last_success_at),
                    state.last_error_type.value if state.last_error_type else None,
                    project_id,
                    generation_id,
                    state.source_type.value,
                    state.source_id_hash,
                ),
            )
            if updated.rowcount != 1:
                raise SyncConflict("source_state_envelope_mismatch")
        completed = self._complete_retrieval_requests(
            connection,
            candidate,
        )
        connection.execute(
            """
            UPDATE project_contexts
            SET payload_json = ?
            WHERE project_id = ?
            """,
            (
                candidate.envelope.context.model_dump_json(),
                project_id,
            ),
        )
        connection.execute(
            """
            UPDATE project_sync_generations
            SET source_cursor = ?, context_json = ?
            WHERE project_id = ? AND generation_id = ?
            """,
            (
                candidate.envelope.source_cursor,
                _canonical_json(
                    candidate.envelope.context.model_dump(mode="json")
                ),
                project_id,
                generation_id,
            ),
        )
        self._insert_audit(connection, candidate.audit, "unchanged")
        self._record_successful_sync(connection, candidate.audit.finished_at)
        return SyncCommitResult(
            outcome="unchanged",
            generation_id=generation_id,
            source_cursor=candidate.envelope.source_cursor,
            completed_retrieval_request_ids=completed,
        )

    @staticmethod
    def _record_successful_sync(
        connection: sqlite3.Connection,
        finished_at: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE project_sync_clock_state
            SET trusted_wall_at = ?, last_observed_wall_at = ?,
                clock_untrusted = 0,
                needs_sync = 0, reason = 'normal'
            WHERE singleton_id = 1
            """,
            (_datetime_text(finished_at), _datetime_text(finished_at)),
        )

    def _build_effective_records(
        self,
        connection: sqlite3.Connection,
        candidate: SyncCommit,
        prior_generation_id: str | None,
    ) -> tuple[
        tuple[SourceState, ...],
        tuple[ProtectedSourceRecord, ...],
        tuple[ProtectedChunkRecord, ...],
    ]:
        project_id = candidate.envelope.project_id
        prior_states: dict[tuple[SyncSourceType, str], SourceState] = {}
        prior_sources: dict[tuple[SyncSourceType, str], ProtectedSourceRecord] = {}
        prior_chunks: dict[
            tuple[SyncSourceType, str], list[ProtectedChunkRecord]
        ] = {}
        if prior_generation_id is not None:
            state_rows = connection.execute(
                """
                SELECT * FROM project_source_states
                WHERE project_id = ? AND generation_id = ?
                """,
                (project_id, prior_generation_id),
            ).fetchall()
            for row in state_rows:
                key = (SyncSourceType(row["source_type"]), str(row["source_id_hash"]))
                prior_states[key] = _state_from_row(row)
                if row["protected_source_id"] is not None:
                    prior_sources[key] = _protected_source_from_row(row)
            chunk_rows = connection.execute(
                """
                SELECT * FROM project_evidence_chunks
                WHERE project_id = ? AND generation_id = ?
                ORDER BY ordinal, chunk_id
                """,
                (project_id, prior_generation_id),
            ).fetchall()
            for row in chunk_rows:
                item = _protected_chunk_from_row(row)
                prior_chunks.setdefault(
                    (item.source_type, item.source_id_hash), []
                ).append(item)

        supplied_sources = {
            (item.source_type, item.source_id_hash): item
            for item in candidate.protected_sources
        }
        supplied_chunks: dict[
            tuple[SyncSourceType, str], list[ProtectedChunkRecord]
        ] = {}
        for item in candidate.protected_chunks:
            supplied_chunks.setdefault(
                (item.source_type, item.source_id_hash), []
            ).append(item)

        states: list[SourceState] = []
        sources: list[ProtectedSourceRecord] = []
        chunks: list[ProtectedChunkRecord] = []
        for state in candidate.source_states:
            key = (state.source_type, state.source_id_hash)
            if state.status in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED}:
                states.append(state)
                continue
            if state.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}:
                prior_state = prior_states.get(key)
                prior_source = prior_sources.get(key)
                source_chunks = prior_chunks.get(key, [])
                if prior_state is not None and prior_state.status in {
                    SourceSyncStatus.ACTIVE,
                    SourceSyncStatus.FAILED,
                    SourceSyncStatus.STALE,
                }:
                    if not self._can_reuse_source(
                        state,
                        prior_state,
                        prior_source,
                        source_chunks,
                    ):
                        raise SyncConflict("source_reuse_conflict")
                    assert prior_state is not None
                    assert prior_source is not None
                    try:
                        state = SourceState(
                            project_id=state.project_id,
                            source_type=state.source_type,
                            source_id_hash=state.source_id_hash,
                            source_version=prior_state.source_version,
                            content_hash=prior_state.content_hash,
                            permission_hash=state.permission_hash,
                            status=state.status,
                            last_attempt_at=state.last_attempt_at,
                            last_success_at=prior_state.last_success_at,
                            last_error_type=state.last_error_type,
                        )
                    except ValueError:
                        raise SyncConflict("source_reuse_conflict") from None
                    source = prior_source
                elif prior_source is not None or source_chunks:
                    raise SyncConflict("source_reuse_conflict")
                elif state.status is SourceSyncStatus.STALE:
                    raise SyncConflict("source_reuse_conflict")
                else:
                    source = supplied_sources.get(key)
                    source_chunks = supplied_chunks.get(key, [])
            else:
                source = supplied_sources.get(key)
                source_chunks = supplied_chunks.get(key, [])
            if source is None:
                if state.status in {
                    SourceSyncStatus.FAILED,
                    SourceSyncStatus.STALE,
                }:
                    states.append(state)
                    continue
                raise ValueError("protected_source_missing")
            states.append(state)
            sources.append(source)
            chunks.extend(source_chunks)
        return tuple(states), tuple(sources), tuple(chunks)

    @staticmethod
    def _can_reuse_source(
        candidate: SourceState,
        prior_state: SourceState | None,
        prior_source: ProtectedSourceRecord | None,
        prior_chunks: list[ProtectedChunkRecord],
    ) -> bool:
        if prior_state is None or prior_source is None:
            return False
        if prior_state.status not in {
            SourceSyncStatus.ACTIVE,
            SourceSyncStatus.FAILED,
            SourceSyncStatus.STALE,
        }:
            return False
        if (
            prior_state.source_version is None
            or prior_state.content_hash is None
            or prior_state.last_success_at is None
            or candidate.permission_hash != prior_state.permission_hash
            or prior_source.source_version != prior_state.source_version
            or prior_source.content_hash != prior_state.content_hash
            or prior_source.permission_hash != prior_state.permission_hash
            or prior_source.source_time is None
            or not prior_source.protected_source_id
            or not prior_source.protected_title
            or not prior_source.protected_url
        ):
            return False
        if (
            candidate.status is SourceSyncStatus.STALE
            and (
                candidate.source_version != prior_state.source_version
                or candidate.content_hash != prior_state.content_hash
            )
        ):
            return False
        if candidate.source_type in {
            SyncSourceType.DOCUMENT,
            SyncSourceType.MEETING_NOTE,
        } and not prior_chunks:
            return False
        if any(
            chunk.source_type is not candidate.source_type
            or chunk.source_id_hash != candidate.source_id_hash
            or chunk.source_version != prior_state.source_version
            or chunk.end_offset <= chunk.start_offset
            or not chunk.protected_heading_path
            or not chunk.protected_text
            for chunk in prior_chunks
        ):
            return False
        return True

    @staticmethod
    def _insert_source_states(
        connection: sqlite3.Connection,
        project_id: str,
        generation_id: str,
        states: tuple[SourceState, ...],
        sources: tuple[ProtectedSourceRecord, ...],
    ) -> None:
        protected = {
            (item.source_type, item.source_id_hash): item for item in sources
        }
        for state in states:
            source = protected.get((state.source_type, state.source_id_hash))
            connection.execute(
                """
                INSERT INTO project_source_states(
                    project_id, generation_id, source_type, source_id_hash,
                    source_version, source_time, content_hash, permission_hash, status,
                    last_attempt_at, last_success_at, last_error_type,
                    protected_source_id, protected_title, protected_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    generation_id,
                    state.source_type.value,
                    state.source_id_hash,
                    state.source_version,
                    _optional_datetime_text(source.source_time) if source else None,
                    state.content_hash,
                    state.permission_hash,
                    state.status.value,
                    _datetime_text(state.last_attempt_at),
                    _optional_datetime_text(state.last_success_at),
                    state.last_error_type.value if state.last_error_type else None,
                    source.protected_source_id if source else None,
                    source.protected_title if source else None,
                    source.protected_url if source else None,
                ),
            )

    @staticmethod
    def _insert_chunks(
        connection: sqlite3.Connection,
        project_id: str,
        generation_id: str,
        chunks: tuple[ProtectedChunkRecord, ...],
    ) -> None:
        for item in chunks:
            connection.execute(
                """
                INSERT INTO project_evidence_chunks(
                    project_id, generation_id, chunk_id, source_type,
                    source_id_hash, source_version, ordinal,
                    protected_heading_path, protected_text, start_offset,
                    end_offset, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    generation_id,
                    item.chunk_id,
                    item.source_type.value,
                    item.source_id_hash,
                    item.source_version,
                    item.ordinal,
                    item.protected_heading_path,
                    item.protected_text,
                    item.start_offset,
                    item.end_offset,
                    item.content_hash,
                ),
            )

    def _complete_retrieval_requests(
        self,
        connection: sqlite3.Connection,
        candidate: SyncCommit,
    ) -> tuple[str, ...]:
        completed: list[str] = []
        for request_id in candidate.envelope.completed_retrieval_request_ids:
            row = connection.execute(
                """
                SELECT * FROM project_retrieval_requests
                WHERE project_id = ? AND request_id = ?
                """,
                (candidate.envelope.project_id, request_id),
            ).fetchone()
            if row is None or row["status"] not in {
                RetrievalRequestStatus.PENDING.value,
                RetrievalRequestStatus.IN_PROGRESS.value,
                RetrievalRequestStatus.COMPLETED.value,
            }:
                raise SyncConflict("retrieval_evidence_missing")
            if row["status"] == RetrievalRequestStatus.COMPLETED.value:
                completed.append(request_id)
                continue
            requested_hashes = set(json.loads(row["source_id_hashes_json"]))
            baseline_generation_id = row["baseline_generation_id"]
            baseline_content_hash = row["baseline_content_hash"]
            baseline_source_cursor = row["baseline_source_cursor"]
            baseline_sources_json = row["baseline_sources_json"]
            if (
                baseline_generation_id is None
                or baseline_content_hash is None
                or baseline_source_cursor is None
                or baseline_sources_json is None
                or candidate.generation_id == baseline_generation_id
                or candidate.envelope.content_hash == baseline_content_hash
                or candidate.envelope.source_cursor <= int(baseline_source_cursor)
                or candidate.audit.finished_at >= _parse_datetime(row["expires_at"])
            ):
                raise SyncConflict("retrieval_evidence_missing")
            try:
                baseline_sources = tuple(
                    RetrievalSourceBaseline.model_validate(item)
                    for item in json.loads(baseline_sources_json)
                )
            except (TypeError, ValueError):
                raise SyncConflict("retrieval_evidence_missing") from None
            current_sources = self._candidate_source_baselines(
                candidate,
                tuple(sorted(requested_hashes)),
            )
            baseline_by_hash = {
                item.source_id_hash: item for item in baseline_sources
            }
            if set(baseline_by_hash) != requested_hashes:
                raise SyncConflict("retrieval_evidence_missing")
            for current in current_sources:
                baseline = baseline_by_hash[current.source_id_hash]
                if (
                    current.content_hash == baseline.content_hash
                    and current.chunk_fingerprint
                    == baseline.chunk_fingerprint
                ):
                    raise SyncConflict("retrieval_evidence_missing")
            updated = connection.execute(
                """
                UPDATE project_retrieval_requests
                SET status = ?, completed_at = ?, lease_expires_at = NULL
                WHERE project_id = ? AND request_id = ?
                  AND status IN (?, ?)
                """,
                (
                    RetrievalRequestStatus.COMPLETED.value,
                    _datetime_text(candidate.audit.finished_at),
                    candidate.envelope.project_id,
                    request_id,
                    RetrievalRequestStatus.PENDING.value,
                    RetrievalRequestStatus.IN_PROGRESS.value,
                ),
            )
            if updated.rowcount != 1:
                raise SyncConflict("retrieval_evidence_missing")
            completed.append(request_id)
        return tuple(completed)

    @staticmethod
    def _candidate_source_baselines(
        candidate: SyncCommit,
        source_id_hashes: tuple[str, ...],
    ) -> tuple[RetrievalSourceBaseline, ...]:
        states = {
            item.source_id_hash: item for item in candidate.source_states
        }
        protected_hashes = {
            item.source_id_hash for item in candidate.protected_sources
        }
        chunks: dict[str, list[tuple[int, str]]] = {}
        for item in candidate.protected_chunks:
            chunks.setdefault(item.source_id_hash, []).append(
                (item.ordinal, item.content_hash)
            )
        baselines: list[RetrievalSourceBaseline] = []
        for source_id_hash in source_id_hashes:
            state = states.get(source_id_hash)
            source_chunks = sorted(chunks.get(source_id_hash, []))
            if (
                state is None
                or state.status is not SourceSyncStatus.ACTIVE
                or source_id_hash not in protected_hashes
                or state.source_version is None
                or state.content_hash is None
                or (
                    state.source_type
                    in {
                        SyncSourceType.DOCUMENT,
                        SyncSourceType.MEETING_NOTE,
                    }
                    and not source_chunks
                )
            ):
                raise SyncConflict("retrieval_evidence_missing")
            baselines.append(
                RetrievalSourceBaseline(
                    source_id_hash=source_id_hash,
                    source_version=state.source_version,
                    content_hash=state.content_hash,
                    chunk_fingerprint=_chunk_fingerprint(source_chunks),
                )
            )
        return tuple(baselines)

    @staticmethod
    def _source_baselines_for_generation(
        connection: sqlite3.Connection,
        project_id: str,
        generation_id: str,
        source_id_hashes: tuple[str, ...],
    ) -> tuple[RetrievalSourceBaseline, ...]:
        placeholders = ", ".join("?" for _ in source_id_hashes)
        rows = connection.execute(
            f"""
            SELECT source_type, source_id_hash, source_version, content_hash,
                   status, protected_source_id
            FROM project_source_states
            WHERE project_id = ? AND generation_id = ?
              AND source_id_hash IN ({placeholders})
            """,
            (project_id, generation_id, *source_id_hashes),
        ).fetchall()
        states = {str(row["source_id_hash"]): row for row in rows}
        chunk_rows = connection.execute(
            f"""
            SELECT source_id_hash, ordinal, content_hash
            FROM project_evidence_chunks
            WHERE project_id = ? AND generation_id = ?
              AND source_id_hash IN ({placeholders})
            ORDER BY source_id_hash, ordinal, content_hash
            """,
            (project_id, generation_id, *source_id_hashes),
        ).fetchall()
        chunks: dict[str, list[tuple[int, str]]] = {}
        for row in chunk_rows:
            chunks.setdefault(str(row["source_id_hash"]), []).append(
                (int(row["ordinal"]), str(row["content_hash"]))
            )
        baselines: list[RetrievalSourceBaseline] = []
        for source_id_hash in sorted(source_id_hashes):
            row = states.get(source_id_hash)
            source_chunks = chunks.get(source_id_hash, [])
            if (
                row is None
                or row["status"] != SourceSyncStatus.ACTIVE.value
                or row["protected_source_id"] is None
                or row["source_version"] is None
                or row["content_hash"] is None
                or (
                    row["source_type"]
                    in {
                        SyncSourceType.DOCUMENT.value,
                        SyncSourceType.MEETING_NOTE.value,
                    }
                    and not source_chunks
                )
            ):
                raise SyncConflict("retrieval_source_unavailable")
            baselines.append(
                RetrievalSourceBaseline(
                    source_id_hash=source_id_hash,
                    source_version=str(row["source_version"]),
                    content_hash=str(row["content_hash"]),
                    chunk_fingerprint=_chunk_fingerprint(source_chunks),
                )
            )
        return tuple(baselines)

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        audit: SyncAudit,
        outcome: Literal["applied", "unchanged", "degraded"],
    ) -> None:
        source_counts = {
            key.value: value for key, value in audit.source_counts_by_status.items()
        }
        connection.execute(
            """
            INSERT INTO project_sync_audits(
                sync_id, project_id, started_at, finished_at, outcome,
                source_counts_json, chunk_count, duration_ms, error_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit.sync_id,
                audit.project_id,
                _datetime_text(audit.started_at),
                _datetime_text(audit.finished_at),
                outcome,
                _canonical_json(source_counts),
                audit.chunk_count,
                audit.duration_ms,
                audit.error_type,
            ),
        )

    @staticmethod
    def _already_completed_request_ids(
        connection: sqlite3.Connection,
        candidate: SyncCommit,
    ) -> tuple[str, ...]:
        completed: list[str] = []
        for request_id in candidate.envelope.completed_retrieval_request_ids:
            row = connection.execute(
                """
                SELECT status FROM project_retrieval_requests
                WHERE project_id = ? AND request_id = ?
                """,
                (candidate.envelope.project_id, request_id),
            ).fetchone()
            if row is not None and row["status"] == RetrievalRequestStatus.COMPLETED:
                completed.append(request_id)
        return tuple(completed)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chunk_fingerprint(chunks: list[tuple[int, str]]) -> str:
    return hashlib.sha256(
        _canonical_json(chunks).encode("utf-8")
    ).hexdigest()


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_datetime_text(value: datetime | None) -> str | None:
    return _datetime_text(value) if value is not None else None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_parse_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _clock_state_from_row(row: sqlite3.Row) -> SharedClockState:
    reason = str(row["reason"])
    if reason not in {"normal", "resume_detected", "clock_rollback"}:
        raise RuntimeError("clock_state_invalid")
    return SharedClockState(
        trusted_wall_at=_optional_parse_datetime(row["trusted_wall_at"]),
        last_observed_wall_at=_optional_parse_datetime(
            row["last_observed_wall_at"]
        ),
        clock_untrusted=bool(row["clock_untrusted"]),
        needs_sync=bool(row["needs_sync"]),
        reason=reason,
    )


def _state_from_row(row: sqlite3.Row) -> SourceState:
    return SourceState(
        project_id=row["project_id"],
        source_type=row["source_type"],
        source_id_hash=row["source_id_hash"],
        source_version=row["source_version"],
        content_hash=row["content_hash"],
        permission_hash=row["permission_hash"],
        status=row["status"],
        last_attempt_at=_parse_datetime(row["last_attempt_at"]),
        last_success_at=_optional_parse_datetime(row["last_success_at"]),
        last_error_type=row["last_error_type"],
    )


def _protected_source_from_row(row: sqlite3.Row) -> ProtectedSourceRecord:
    return ProtectedSourceRecord(
        source_type=SyncSourceType(row["source_type"]),
        source_id_hash=row["source_id_hash"],
        protected_source_id=bytes(row["protected_source_id"]),
        protected_title=bytes(row["protected_title"]),
        protected_url=bytes(row["protected_url"]),
        source_version=row["source_version"],
        source_time=_optional_parse_datetime(row["source_time"]),
        permission_hash=row["permission_hash"],
        content_hash=row["content_hash"],
    )


def _protected_chunk_from_row(row: sqlite3.Row) -> ProtectedChunkRecord:
    return ProtectedChunkRecord(
        chunk_id=row["chunk_id"],
        source_type=SyncSourceType(row["source_type"]),
        source_id_hash=row["source_id_hash"],
        source_version=row["source_version"],
        ordinal=int(row["ordinal"]),
        protected_heading_path=bytes(row["protected_heading_path"]),
        protected_text=bytes(row["protected_text"]),
        start_offset=int(row["start_offset"]),
        end_offset=int(row["end_offset"]),
        content_hash=row["content_hash"],
    )


def _retrieval_values(request: RetrievalRequest) -> tuple[object, ...]:
    return (
        request.request_id,
        request.project_id,
        request.query_hash,
        _canonical_json(request.source_id_hashes),
        request.baseline_generation_id,
        request.baseline_content_hash,
        request.baseline_source_cursor,
        _canonical_json(
            [item.model_dump(mode="json") for item in request.baseline_sources]
        )
        if request.baseline_sources
        else None,
        request.status.value,
        _datetime_text(request.created_at),
        _datetime_text(request.expires_at),
        _optional_datetime_text(request.lease_expires_at),
        request.attempt_count,
        _optional_datetime_text(request.completed_at),
    )


def _retrieval_from_row(row: sqlite3.Row) -> RetrievalRequest:
    return RetrievalRequest(
        request_id=row["request_id"],
        project_id=row["project_id"],
        query_hash=row["query_hash"],
        source_id_hashes=tuple(json.loads(row["source_id_hashes_json"])),
        baseline_generation_id=row["baseline_generation_id"],
        baseline_content_hash=row["baseline_content_hash"],
        baseline_source_cursor=(
            int(row["baseline_source_cursor"])
            if row["baseline_source_cursor"] is not None
            else None
        ),
        baseline_sources=(
            tuple(
                RetrievalSourceBaseline.model_validate(item)
                for item in json.loads(row["baseline_sources_json"])
            )
            if row["baseline_sources_json"] is not None
            else ()
        ),
        status=row["status"],
        created_at=_parse_datetime(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        lease_expires_at=_optional_parse_datetime(row["lease_expires_at"]),
        attempt_count=int(row["attempt_count"]),
        completed_at=_optional_parse_datetime(row["completed_at"]),
    )
