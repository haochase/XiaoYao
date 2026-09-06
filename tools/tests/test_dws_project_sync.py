from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import multiprocessing
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from threading import BrokenBarrierError
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

import pytest
from pydantic import ValidationError

from companion_gateway.project.models import (
    EvidenceRef,
    ProjectContextPackage,
    SourcedFact,
)
from companion_gateway.project.sync_models import SourceErrorType, SyncSourceType
import tools.dws_project_sync as sync_cli
import tools.dws_sync.state_lock as state_lock
from tools.dws_project_sync import (
    QwenProjectContextArtifact,
    SyncCliState,
    build_envelope,
    main,
    source_bundle_semantic_hash,
)
from tools.dws_sync import (
    DwsProjectManifest,
    DwsRetrievalRequest,
    DwsRetrievalSource,
    DwsSourceBundle,
    DwsSourceRecord,
    DwsSourceSpec,
    lifecycle,
)


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SCOPE = "project:project-1"


@pytest.fixture(autouse=True)
def isolated_lifecycle_root(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "dws-sync-locks"
    monkeypatch.setattr(state_lock, "PRIVATE_LOCK_ROOT", root)
    monkeypatch.setattr(sync_cli, "LIFECYCLE_ROOT", root)


def canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_spec(source_type: str, source_id: str) -> DwsSourceSpec:
    data: dict[str, object] = {
        "source_type": source_type,
        "source_id": source_id,
    }
    if source_type == "calendar":
        data.update(
            window_start="2026-09-05T08:00:00+00:00",
            window_end="2026-09-05T18:00:00+00:00",
        )
    return DwsSourceSpec.model_validate(data)


def project(*, sources: tuple[DwsSourceSpec, ...] | None = None):
    return DwsProjectManifest(
        project_id="project-1",
        project_name="测试项目",
        profile="private-profile",
        permission_scope=SCOPE,
        sources=sources or (source_spec("document", "doc-1"),),
    )


def active_record(
    *,
    source_type: str = "document",
    source_id: str = "doc-1",
    content: str = "# 决策\n采用方案 B。",
) -> DwsSourceRecord:
    return DwsSourceRecord(
        source_type=source_type,
        source_id=source_id,
        permission_scope=SCOPE,
        fetched_at=NOW,
        status="active",
        source_title="决策文档",
        source_url=f"dingtalk://{source_type}/{source_id}",
        source_version="v1",
        source_time=NOW,
        content_text=content,
        attributes_json="{}",
        content_hash=digest(content),
    )


def bundle(*records: DwsSourceRecord) -> DwsSourceBundle:
    selected = records or (active_record(),)
    payload = {
        "schema_version": 1,
        "project_id": "project-1",
        "project_name": "测试项目",
        "permission_scope": SCOPE,
        "collected_at": NOW.isoformat(),
        "records": [item.model_dump(mode="json") for item in selected],
    }
    return DwsSourceBundle(
        **payload,
        content_hash=digest(canonical(payload)),
    )


def context(*, excerpt: str = "采用 方案 B。") -> ProjectContextPackage:
    record = active_record()
    return ProjectContextPackage.model_validate(
        {
            "project_id": "project-1",
            "project_name": "测试项目",
            "generated_at": NOW.isoformat(),
            "source_refs": [
                {
                    "source_type": record.source_type,
                    "source_id": record.source_id,
                    "source_title": record.source_title,
                    "source_url": record.source_url,
                    "source_time": record.source_time.isoformat(),
                    "excerpt": excerpt,
                    "permission_scope": record.permission_scope,
                }
            ],
            "active_decisions": [],
            "open_actions": [],
            "current_risks": [],
            "next_meeting": None,
            "permission_scope": SCOPE,
            "freshness_seconds": 300,
        }
    )


def sourced_fact(
    *,
    source_type: str = "document",
    source_id: str = "doc-1",
    excerpt: str = "采用 方案 B。",
) -> SourcedFact:
    record = active_record(source_type=source_type, source_id=source_id)
    return SourcedFact(
        text="采用方案 B",
        source_refs=(
            EvidenceRef(
                source_type=record.source_type.value,
                source_id=record.source_id,
                source_title=record.source_title or "",
                source_url=record.source_url or "",
                source_time=record.source_time or NOW,
                excerpt=excerpt,
                permission_scope=record.permission_scope,
            ),
        ),
    )


def write_json(path: Path, payload: object) -> None:
    path.write_text(canonical(payload), encoding="utf-8")


def write_manifest(path: Path, selected: DwsProjectManifest) -> None:
    write_json(
        path,
        {
            "schema_version": 1,
            "projects": [selected.model_dump(mode="json")],
        },
    )


def write_push_inputs(tmp_path: Path) -> dict[str, Path]:
    selected = project()
    paths = {
        "manifest": tmp_path / "manifest.json",
        "sources": tmp_path / "sources.json",
        "context": tmp_path / "context.json",
        "state": tmp_path / "state.json",
    }
    write_manifest(paths["manifest"], selected)
    write_json(paths["sources"], bundle().model_dump(mode="json"))
    artifact = QwenProjectContextArtifact(
        schema_version=1,
        context=context(),
        completed_retrieval_request_ids=(),
    )
    write_json(paths["context"], artifact.model_dump(mode="json"))
    return paths


def push_args(paths: dict[str, Path], *extra: str) -> list[str]:
    return [
        "push",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--context-file",
        str(paths["context"]),
        "--state-file",
        str(paths["state"]),
        "--gateway",
        "http://127.0.0.1:8731",
        *extra,
    ]


def pending_args(paths: dict[str, Path]) -> list[str]:
    return [
        "pending",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--gateway",
        "http://127.0.0.1:8731",
    ]


def artifact_args(paths: dict[str, Path], run_token: str) -> list[str]:
    return [
        "artifact",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--context-file",
        str(paths["context"]),
        "--state-file",
        str(paths["state"]),
        "--run-token",
        run_token,
    ]


def reuse_artifact_args(paths: dict[str, Path], run_token: str) -> list[str]:
    return [
        "reuse-artifact",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--context-file",
        str(paths["context"]),
        "--state-file",
        str(paths["state"]),
        "--run-token",
        run_token,
    ]


def recover_pending_args(
    paths: dict[str, Path], database: Path
) -> list[str]:
    return [
        "recover-pending",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--context-file",
        str(paths["context"]),
        "--state-file",
        str(paths["state"]),
        "--database-file",
        str(database),
    ]


def write_pending_recovery_fixture(
    tmp_path: Path,
) -> tuple[dict[str, Path], Path, QwenProjectContextArtifact]:
    paths = write_push_inputs(tmp_path)
    selected = bundle()
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    active = sync_cli._build_envelope(
        project(),
        selected,
        approved.context,
        completed_retrieval_request_ids=(),
        source_cursor=1,
        now=NOW,
    )
    pending = sync_cli._build_envelope(
        project(),
        selected,
        approved.context,
        completed_retrieval_request_ids=(),
        source_cursor=2,
        now=NOW,
    )
    write_json(paths["sources"], selected.model_dump(mode="json"))
    write_json(paths["context"], {"old": "current"})
    write_json(
        paths["context"].with_name("context.approved.json"),
        {"old": "approved"},
    )
    state = semantic_state(
        selected,
        approved,
        pending={
            "source_cursor": 2,
            "content_hash": pending.content_hash,
            "sync_id": pending.sync_id,
            "completion_claims_hash": digest("[]"),
        },
    )
    state["last_content_hash"] = active.content_hash
    state["last_sync_id"] = "older-client-sync-id"
    write_json(paths["state"], state)

    database = tmp_path / "companion.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE project_sync_generations (
                project_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                sync_id TEXT NOT NULL,
                source_cursor INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                completion_claims_hash TEXT NOT NULL,
                context_json TEXT NOT NULL
            );
            CREATE TABLE project_active_generations (
                project_id TEXT NOT NULL,
                generation_id TEXT NOT NULL
            );
            CREATE TABLE project_source_states (
                project_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id_hash TEXT NOT NULL,
                source_version TEXT,
                source_time TEXT,
                content_hash TEXT,
                permission_hash TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE project_sync_audits (
                sync_id TEXT NOT NULL,
                project_id TEXT NOT NULL
            );
            CREATE TABLE project_retrieval_requests (
                request_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO project_sync_generations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "project-1",
                "generation-1",
                active.sync_id,
                1,
                active.content_hash,
                digest("[]"),
                canonical(approved.context.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO project_active_generations VALUES (?, ?)",
            ("project-1", "generation-1"),
        )
        record = selected.records[0]
        connection.execute(
            "INSERT INTO project_source_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "project-1",
                "generation-1",
                record.source_type.value,
                digest(record.source_id),
                record.source_version,
                record.source_time.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                record.content_hash,
                digest(record.permission_scope),
                record.status,
            ),
        )
    connection.close()
    return paths, database, approved


def recovery_snapshot(
    paths: dict[str, Path], database: Path
) -> dict[Path, tuple[bool, bytes]]:
    watched = (
        paths["context"].with_name("context.approved.json"),
        paths["context"],
        paths["state"],
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    )
    return {
        path: (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in watched
    }


def database_snapshot(database: Path) -> dict[Path, tuple[bool, bytes]]:
    return {
        path: (path.exists(), path.read_bytes() if path.exists() else b"")
        for path in (
            database,
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
            Path(str(database) + "-journal"),
        )
    }


def test_recover_pending_rebuilds_active_artifacts_and_checkpoints(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths, database, expected = write_pending_recovery_fixture(tmp_path)
    database_before = recovery_snapshot(paths, database)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    connections: list[sqlite3.Connection] = []
    statements: list[str] = []
    real_connect = sync_cli.sqlite3.connect

    def tracked_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sync_cli.sqlite3, "connect", tracked_connect)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "pending_recovered",
        "project_id": "project-1",
        "active_cursor": 1,
        "abandoned_pending_cursor": 2,
    }
    for path in (
        paths["context"].with_name("context.approved.json"),
        paths["context"],
    ):
        assert QwenProjectContextArtifact.model_validate_json(
            path.read_bytes()
        ) == expected
    state = SyncCliState.model_validate_json(paths["state"].read_bytes())
    assert state.last_cursor == 1
    assert state.last_sync_id == "older-client-sync-id"
    assert state.pending is None
    assert state.last_source_semantic_hash == source_bundle_semantic_hash(
        bundle()
    )
    assert state.last_artifact_hash == artifact_hash(expected)
    assert calls == [((database.resolve().as_uri() + "?mode=ro",), {"uri": True})]
    assert "immutable" not in calls[0][0][0]
    assert statements[:2] == ["PRAGMA query_only=ON", "BEGIN"]
    assert not any(
        statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER")
        )
        for statement in statements
    )
    after = recovery_snapshot(paths, database)
    for path in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        assert after[path] == database_before[path]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_recover_pending_allows_drifted_pending_identity(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    pending_hash = "b" * 64
    state.update(last_cursor=4)
    state["pending"] = {
        "source_cursor": 5,
        "content_hash": pending_hash,
        "sync_id": sync_cli._sync_id("project-1", 5, pending_hash),
        "completion_claims_hash": digest("[]"),
    }
    write_json(paths["state"], state)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE project_sync_generations SET source_cursor = 4"
        )
    connection.close()

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 0

    assert json.loads(capsys.readouterr().out) == {
        "status": "pending_recovered",
        "project_id": "project-1",
        "active_cursor": 4,
        "abandoned_pending_cursor": 5,
    }
    recovered = SyncCliState.model_validate_json(paths["state"].read_bytes())
    assert recovered.last_cursor == 4
    assert recovered.pending is None


def test_recover_pending_recreates_missing_approved_and_current_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, expected = write_pending_recovery_fixture(tmp_path)
    paths["context"].unlink()
    paths["context"].with_name("context.approved.json").unlink()

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "pending_recovered"
    for path in (
        paths["context"].with_name("context.approved.json"),
        paths["context"],
    ):
        assert QwenProjectContextArtifact.model_validate_json(
            path.read_bytes()
        ) == expected


def test_recover_pending_denies_missing_pending_without_any_write(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    state["pending"] = None
    write_json(paths["state"], state)
    before = recovery_snapshot(paths, database)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    assert recovery_snapshot(paths, database) == before


@pytest.mark.parametrize(
    "case",
    [
        "pending_cursor",
        "last_hash",
        "pending_hash",
        "pending_sync_id",
        "active_missing",
        "active_cursor",
        "active_duplicate",
        "pending_generation",
        "pending_sync_other_project",
        "pending_audit",
        "pending_audit_other_project",
        "claims",
        "bundle_retrieval",
        "in_progress_retrieval",
        "bundle_status",
        "source_identity",
        "source_scope",
        "source_version",
        "source_time",
        "source_content",
        "source_status",
        "context_evidence",
        "context_project",
    ],
)
def test_recover_pending_denial_matrix_preserves_all_files(
    tmp_path: Path,
    capsys,
    case: str,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    pending = state["pending"]
    assert isinstance(pending, dict)
    if case == "pending_cursor":
        pending["source_cursor"] = 3
        write_json(paths["state"], state)
    elif case == "last_hash":
        state["last_content_hash"] = "b" * 64
        write_json(paths["state"], state)
    elif case == "pending_hash":
        pending["content_hash"] = "b" * 64
        write_json(paths["state"], state)
    elif case == "pending_sync_id":
        pending["sync_id"] = "wrong-pending-sync"
        write_json(paths["state"], state)
    elif case == "claims":
        pending["completion_claims_hash"] = "b" * 64
        write_json(paths["state"], state)
    elif case == "bundle_retrieval":
        selected = DwsSourceBundle.model_validate_json(
            paths["sources"].read_bytes()
        )
        retrieval = DwsRetrievalRequest(
            request_id="request-1",
            query_hash="d" * 64,
            request_epoch=1,
            attempt_count=1,
            lease_expires_at=NOW + timedelta(minutes=5),
            lease_token="x" * 32,
            sources=(
                DwsRetrievalSource(
                    source_type="document", source_id="doc-1"
                ),
            ),
        )
        write_json(
            paths["sources"],
            rehash_bundle(
                selected.model_copy(update={"retrieval_requests": (retrieval,)})
            ).model_dump(mode="json"),
        )
    elif case == "bundle_status":
        selected = bundle(
            DwsSourceRecord(
                source_type="document",
                source_id="doc-1",
                permission_scope=SCOPE,
                fetched_at=NOW,
                status="failed",
                error_type="provider_unavailable",
                retryable=True,
            )
        )
        write_json(paths["sources"], selected.model_dump(mode="json"))
    else:
        with sqlite3.connect(database) as connection:
            if case == "active_missing":
                connection.execute("DELETE FROM project_active_generations")
            elif case == "active_cursor":
                connection.execute(
                    "UPDATE project_sync_generations SET source_cursor = 9"
                )
            elif case == "active_duplicate":
                row = connection.execute(
                    "SELECT * FROM project_sync_generations"
                ).fetchone()
                connection.execute(
                    "INSERT INTO project_sync_generations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row[0], "generation-2", *row[2:]),
                )
                connection.execute(
                    "INSERT INTO project_active_generations VALUES (?, ?)",
                    ("project-1", "generation-2"),
                )
            elif case == "pending_generation":
                row = connection.execute(
                    "SELECT * FROM project_sync_generations"
                ).fetchone()
                connection.execute(
                    "INSERT INTO project_sync_generations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row[0],
                        "generation-2",
                        pending["sync_id"],
                        pending["source_cursor"],
                        row[4],
                        row[5],
                        row[6],
                    ),
                )
            elif case == "pending_sync_other_project":
                row = connection.execute(
                    "SELECT * FROM project_sync_generations"
                ).fetchone()
                connection.execute(
                    "INSERT INTO project_sync_generations VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "project-2",
                        "generation-2",
                        pending["sync_id"],
                        1,
                        row[4],
                        row[5],
                        row[6],
                    ),
                )
            elif case == "pending_audit":
                connection.execute(
                    "INSERT INTO project_sync_audits VALUES (?, ?)",
                    (pending["sync_id"], "project-1"),
                )
            elif case == "pending_audit_other_project":
                connection.execute(
                    "INSERT INTO project_sync_audits VALUES (?, ?)",
                    (pending["sync_id"], "project-2"),
                )
            elif case == "in_progress_retrieval":
                connection.execute(
                    "INSERT INTO project_retrieval_requests VALUES (?, ?, ?)",
                    ("request-1", "project-1", "in_progress"),
                )
            elif case == "source_identity":
                connection.execute(
                    "UPDATE project_source_states SET source_id_hash = ?",
                    ("b" * 64,),
                )
            elif case == "source_scope":
                connection.execute(
                    "UPDATE project_source_states SET permission_hash = ?",
                    ("b" * 64,),
                )
            elif case == "source_version":
                connection.execute(
                    "UPDATE project_source_states SET source_version = 'v2'"
                )
            elif case == "source_time":
                connection.execute(
                    "UPDATE project_source_states SET source_time = ?",
                    ((NOW + timedelta(minutes=1)).isoformat(),),
                )
            elif case == "source_content":
                connection.execute(
                    "UPDATE project_source_states SET content_hash = ?",
                    ("b" * 64,),
                )
            elif case == "source_status":
                connection.execute(
                    "UPDATE project_source_states SET status = 'failed'"
                )
            elif case == "context_evidence":
                invalid_context = context(excerpt="不存在的证据片段")
                connection.execute(
                    "UPDATE project_sync_generations SET context_json = ?",
                    (canonical(invalid_context.model_dump(mode="json")),),
                )
            elif case == "context_project":
                invalid_context = context().model_copy(
                    update={"project_id": "project-2"}
                )
                connection.execute(
                    "UPDATE project_sync_generations SET context_json = ?",
                    (canonical(invalid_context.model_dump(mode="json")),),
                )
            else:
                raise AssertionError(case)
        connection.close()
    before = recovery_snapshot(paths, database)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    assert recovery_snapshot(paths, database) == before


@pytest.mark.parametrize("failed_index", [1, 2])
@pytest.mark.parametrize("existing_artifacts", [False, True])
def test_recover_pending_rolls_back_all_three_files_on_later_apply_failure(
    tmp_path: Path,
    capsys,
    monkeypatch,
    failed_index: int,
    existing_artifacts: bool,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    if not existing_artifacts:
        paths["context"].unlink()
        paths["context"].with_name("context.approved.json").unlink()
    before = recovery_snapshot(paths, database)
    ordered_paths: list[Path] = []
    real_apply = sync_cli._RecoverableAtomicWrite.apply

    def fail_after_replace(operation) -> None:  # type: ignore[no-untyped-def]
        ordered_paths.append(operation._path)
        real_apply(operation)
        if len(ordered_paths) - 1 == failed_index:
            raise RuntimeError("private-apply-detail")

    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "apply",
        fail_after_replace,
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "sync_failed",
    }
    assert ordered_paths == [
        paths["context"].with_name("context.approved.json"),
        paths["context"],
        paths["state"],
    ][: failed_index + 1]
    assert recovery_snapshot(paths, database) == before


def test_recover_pending_rejects_database_alias_before_open(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    database.unlink()
    os.link(paths["manifest"], database)
    before = recovery_snapshot(paths, database)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "database_file_not_regular_file",
    }
    assert recovery_snapshot(paths, database) == before


@pytest.mark.parametrize("offset", [18, 19])
def test_recover_pending_rejects_non_delete_database_header_before_connect(
    tmp_path: Path,
    capsys,
    monkeypatch,
    offset: int,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    raw = bytearray(database.read_bytes())
    raw[offset] = 2
    database.write_bytes(raw)
    before = recovery_snapshot(paths, database)
    monkeypatch.setattr(
        sync_cli.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("database must not be opened"),
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    assert recovery_snapshot(paths, database) == before


def test_recover_pending_rejects_real_wal_without_touching_sidecars(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO project_sync_audits VALUES (?, ?)",
        ("independent-audit", "project-1"),
    )
    writer.commit()
    wal = Path(str(database) + "-wal")
    shm = Path(str(database) + "-shm")
    assert wal.stat().st_size > 0
    assert shm.stat().st_size > 0
    before = recovery_snapshot(paths, database)
    monkeypatch.setattr(
        sync_cli.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("WAL database must not be opened"),
    )
    try:
        assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error",
            "error_type": "pending_recovery_denied",
        }
        assert recovery_snapshot(paths, database) == before
    finally:
        writer.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_recover_pending_rejects_any_existing_database_sidecar(
    tmp_path: Path,
    capsys,
    monkeypatch,
    suffix: str,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    sidecar = Path(str(database) + suffix)
    sidecar.write_bytes(b"existing-sidecar")
    before = recovery_snapshot(paths, database)
    monkeypatch.setattr(
        sync_cli.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("sidecar must prevent open"),
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    assert recovery_snapshot(paths, database) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_recover_pending_rejects_output_path_matching_database_sidecar(
    tmp_path: Path,
    capsys,
    suffix: str,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    original_context = paths["context"]
    paths["context"] = Path(str(database) + suffix)
    before = recovery_snapshot(
        {**paths, "context": original_context}, database
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "database_file_not_regular_file",
    }
    assert recovery_snapshot(
        {**paths, "context": original_context}, database
    ) == before


def test_recover_pending_rejects_output_aliasing_database_sidecar(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    original_context = paths["context"]
    sidecar = Path(str(database) + "-wal")
    sidecar.write_bytes(b"sidecar")
    alias = tmp_path / "sidecar-alias.json"
    os.link(sidecar, alias)
    paths["context"] = alias
    before = recovery_snapshot(
        {**paths, "context": original_context}, database
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "database_file_not_regular_file",
    }
    assert recovery_snapshot(
        {**paths, "context": original_context}, database
    ) == before
    assert alias.read_bytes() == b"sidecar"


def test_recover_pending_rechecks_sidecars_after_database_open(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    private_before = recovery_snapshot(paths, database)
    sidecar = Path(str(database) + "-shm")
    real_connect = sqlite3.connect

    def connect_after_sidecar_appears(*args, **kwargs):  # type: ignore[no-untyped-def]
        sidecar.write_bytes(b"")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        sync_cli.sqlite3,
        "connect",
        connect_after_sidecar_appears,
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    for path in (
        paths["context"].with_name("context.approved.json"),
        paths["context"],
        paths["state"],
        database,
    ):
        assert recovery_snapshot(paths, database)[path] == private_before[path]
    assert sidecar.exists()
    assert sidecar.read_bytes() == b""


def test_recovery_database_guard_allows_sqlite_readonly_connection(
    tmp_path: Path,
) -> None:
    _paths, database, _expected = write_pending_recovery_fixture(tmp_path)

    with sync_cli._recovery_database_guard(database) as handle:
        assert handle
        with sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM project_sync_generations"
            ).fetchone() == (1,)


def test_recover_pending_rejects_existing_writer_without_changes(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("BEGIN IMMEDIATE")
    before = recovery_snapshot(paths, database)
    try:
        assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error",
            "error_type": "pending_recovery_denied",
        }
        assert recovery_snapshot(paths, database) == before
    finally:
        writer.rollback()
        writer.close()


def test_recovery_guard_blocks_writer_switching_to_wal_after_precheck(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    real_connect = sqlite3.connect
    writer_connections: list[sqlite3.Connection] = []
    writer_blocked = False
    source_baseline: dict[Path, tuple[bool, bytes]] = {}

    def racing_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal writer_blocked
        if kwargs.get("uri"):
            writer: sqlite3.Connection | None = None
            try:
                writer = real_connect(database, timeout=0)
                assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == (
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "INSERT INTO project_sync_audits VALUES (?, ?)",
                    ("racing-audit", "project-1"),
                )
                writer.commit()
                writer_connections.append(writer)
                writer = None
            except sqlite3.Error:
                writer_blocked = True
            finally:
                if writer is not None:
                    writer.close()
            source_baseline.update(database_snapshot(database))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sync_cli.sqlite3, "connect", racing_connect)
    try:
        assert main(recover_pending_args(paths, database), now=lambda: NOW) == 0
        assert json.loads(capsys.readouterr().out)["status"] == (
            "pending_recovered"
        )
        assert writer_blocked
        assert database_snapshot(database) == source_baseline
    finally:
        for writer in writer_connections:
            writer.close()


def test_recovery_windows_api_error_is_sanitized_without_changes(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    before = recovery_snapshot(paths, database)

    def fail_open(_path: Path) -> int:
        raise OSError("private CreateFileW detail")

    monkeypatch.setattr(
        sync_cli,
        "_open_recovery_database_handle",
        fail_open,
        raising=False,
    )

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "error",
        "error_type": "pending_recovery_denied",
    }
    assert "private CreateFileW detail" not in output
    assert recovery_snapshot(paths, database) == before


@pytest.mark.parametrize("case", ["proof_denied", "query_error"])
def test_recover_pending_closes_connection_on_denial(
    tmp_path: Path,
    capsys,
    monkeypatch,
    case: str,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        if case == "proof_denied":
            connection.execute(
                "UPDATE project_source_states SET status = 'failed'"
            )
        else:
            connection.execute("DROP TABLE project_sync_audits")
    connection.close()
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        selected = real_connect(*args, **kwargs)
        opened.append(selected)
        return selected

    monkeypatch.setattr(sync_cli.sqlite3, "connect", tracked_connect)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == (
        "pending_recovery_denied"
    )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_recover_pending_refuses_active_lifecycle_without_writes(
    tmp_path: Path,
    capsys,
) -> None:
    paths, database, _expected = write_pending_recovery_fixture(tmp_path)
    started = lifecycle.begin_run(
        "project-1", root=sync_cli.LIFECYCLE_ROOT, now=lambda: NOW
    )
    assert started.status == "started"
    before = recovery_snapshot(paths, database)

    assert main(recover_pending_args(paths, database), now=lambda: NOW) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "lifecycle_active",
    }
    assert recovery_snapshot(paths, database) == before


def artifact_hash(artifact: QwenProjectContextArtifact) -> str:
    return digest(canonical(artifact.model_dump(mode="json")))


def semantic_state(
    selected: DwsSourceBundle,
    approved: QwenProjectContextArtifact,
    *,
    pending: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project-1",
        "last_cursor": 1,
        "last_content_hash": "a" * 64,
        "last_sync_id": "sync-1",
        "pending": pending,
        "last_source_semantic_hash": source_bundle_semantic_hash(selected),
        "last_artifact_hash": artifact_hash(approved),
    }


def rehash_bundle(selected: DwsSourceBundle) -> DwsSourceBundle:
    payload = sync_cli._bundle_hash_payload(selected)
    return DwsSourceBundle(
        **payload,
        content_hash=digest(canonical(payload)),
    )


def start_pending_run() -> str:
    started = lifecycle.begin_run(
        "project-1",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )
    assert started.run_token is not None
    lifecycle.advance_run(
        "project-1",
        started.run_token,
        expected="begun",
        target="collected",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )
    lifecycle.advance_run(
        "project-1",
        started.run_token,
        expected="collected",
        target="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )
    return started.run_token


def test_sync_state_semantic_hashes_are_optional_but_atomic() -> None:
    legacy = SyncCliState(
        schema_version=1,
        project_id="project-1",
        last_cursor=1,
        last_content_hash="a" * 64,
        last_sync_id="sync-1",
        pending=None,
    )
    assert legacy.last_source_semantic_hash is None
    assert legacy.last_artifact_hash is None

    with pytest.raises(ValidationError, match="state_artifact_hashes_invalid"):
        SyncCliState.model_validate(
            {
                **legacy.model_dump(),
                "last_source_semantic_hash": "b" * 64,
            }
        )
    with pytest.raises(ValidationError, match="state_artifact_hashes_invalid"):
        SyncCliState.model_validate(
            {
                **legacy.model_dump(),
                "last_source_semantic_hash": "B" * 64,
                "last_artifact_hash": "c" * 64,
            }
        )
    with pytest.raises(ValidationError, match="state_artifact_hashes_invalid"):
        SyncCliState.model_validate(
            {
                **legacy.model_dump(),
                "last_cursor": 0,
                "last_content_hash": None,
                "last_sync_id": None,
                "last_source_semantic_hash": "b" * 64,
                "last_artifact_hash": "c" * 64,
            }
        )


def test_source_bundle_semantic_hash_is_stable_for_ephemeral_changes_and_order() -> None:
    first = active_record(source_id="doc-1")
    second = active_record(source_id="doc-2", content="第二份来源")
    original = bundle(first, second)
    changed_ephemeral = original.model_copy(
        update={
            "collected_at": NOW + timedelta(minutes=5),
            "records": (
                second.model_copy(
                    update={
                        "fetched_at": NOW + timedelta(minutes=3),
                        "attributes_json": '{"info":{"logId":"request-2"}}',
                    }
                ),
                first.model_copy(
                    update={
                        "fetched_at": NOW + timedelta(minutes=2),
                        "attributes_json": '{"info":{"logId":"request-1"}}',
                    }
                ),
            ),
        }
    )
    assert source_bundle_semantic_hash(original) == source_bundle_semantic_hash(
        changed_ephemeral
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_type", SyncSourceType.CALENDAR),
        ("source_id", "doc-2"),
        ("permission_scope", "project:other"),
        ("source_title", "另一标题"),
        ("source_url", "dingtalk://document/other"),
        ("source_version", "v2"),
        ("source_time", NOW + timedelta(seconds=1)),
        ("content_hash", "f" * 64),
        ("status", "revoked"),
    ],
)
def test_source_bundle_semantic_hash_changes_for_proof_fields(
    field: str,
    value: object,
) -> None:
    original = bundle()
    record = original.records[0]
    updates = {field: value}
    if field == "status":
        updates.update(
            source_title=None,
            source_url=None,
            source_version=None,
            source_time=None,
            content_text=None,
            attributes_json=None,
            content_hash=None,
        )
    changed = original.model_copy(
        update={"records": (record.model_copy(update=updates),)}
    )
    assert source_bundle_semantic_hash(original) != source_bundle_semantic_hash(
        changed
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "project-2"),
        ("project_name", "另一项目"),
        ("permission_scope", "project:other"),
    ],
)
def test_source_bundle_semantic_hash_changes_for_project_identity(
    field: str,
    value: str,
) -> None:
    original = bundle()
    changed = original.model_copy(update={field: value})
    assert source_bundle_semantic_hash(original) != source_bundle_semantic_hash(
        changed
    )


def test_source_bundle_semantic_hash_changes_for_failure_proof_fields() -> None:
    failed = DwsSourceRecord(
        source_type="document",
        source_id="doc-1",
        permission_scope=SCOPE,
        fetched_at=NOW,
        status="failed",
        error_type=SourceErrorType.PROVIDER_UNAVAILABLE,
        retryable=True,
        retry_after_seconds=1,
    )
    original = bundle(failed)
    changed_error = original.model_copy(
        update={
            "records": (
                failed.model_copy(update={"error_type": SourceErrorType.RATE_LIMITED}),
            )
        }
    )
    changed_retryable = original.model_copy(
        update={"records": (failed.model_copy(update={"retryable": False}),)}
    )
    assert source_bundle_semantic_hash(original) != source_bundle_semantic_hash(
        changed_error
    )
    assert source_bundle_semantic_hash(original) != source_bundle_semantic_hash(
        changed_retryable
    )


def test_source_bundle_semantic_hash_excludes_ephemeral_record_fields() -> None:
    failed = DwsSourceRecord(
        source_type="document",
        source_id="doc-1",
        permission_scope=SCOPE,
        fetched_at=NOW,
        status="failed",
        error_type=SourceErrorType.PROVIDER_UNAVAILABLE,
        retryable=True,
        retry_after_seconds=1,
    )
    original = bundle(failed)
    changed = original.model_copy(
        update={
            "records": (
                failed.model_copy(
                    update={
                        "fetched_at": NOW + timedelta(minutes=1),
                        "retry_after_seconds": 30,
                    }
                ),
            )
        }
    )
    assert source_bundle_semantic_hash(original) == source_bundle_semantic_hash(
        changed
    )


def test_recoverable_atomic_write_rejects_hardlinked_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    alias = tmp_path / "alias.json"
    target.write_bytes(b"original")
    try:
        sync_cli.os.link(target, alias)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    transaction = sync_cli._RecoverableAtomicWrite(target, b"replacement")
    with pytest.raises(ValueError, match="private_file_write_failed"):
        transaction.apply()
    assert target.read_bytes() == b"original"
    assert alias.read_bytes() == b"original"


def test_artifact_reuses_approved_context_for_unchanged_source(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    renewed_at = NOW + timedelta(minutes=3)
    selected = rehash_bundle(
        selected.model_copy(
            update={
                "collected_at": renewed_at,
                "records": (
                    selected.records[0].model_copy(update={"fetched_at": renewed_at}),
                ),
            }
        )
    )
    retrieval = DwsRetrievalRequest(
        request_id="request-1",
        query_hash="d" * 64,
        request_epoch=1,
        attempt_count=1,
        lease_expires_at=NOW + timedelta(minutes=5),
        lease_token="x" * 32,
        sources=(DwsRetrievalSource(source_type="document", source_id="doc-1"),),
    )
    selected = rehash_bundle(
        selected.model_copy(update={"retrieval_requests": (retrieval,)})
    )
    write_json(paths["sources"], selected.model_dump(mode="json"))
    approved = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用 方案 B。"),
    )
    approved_path = paths["context"].with_name("context.approved.json")
    write_json(approved_path, approved.model_dump(mode="json"))
    write_json(paths["state"], semantic_state(selected, approved))
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="方案 B。").model_copy(
            update={"generated_at": renewed_at}
        ),
        completed_retrieval_request_ids=("request-1",),
    )
    run_token = start_pending_run()

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_written"
    written = QwenProjectContextArtifact.model_validate_json(
        paths["context"].read_bytes()
    )
    assert written.context == approved.context.model_copy(
        update={"generated_at": renewed_at}
    )
    assert written.completed_retrieval_request_ids == ("request-1",)


def test_reuse_artifact_skips_candidate_for_unchanged_source(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    renewed_at = NOW + timedelta(minutes=3)
    renewed = rehash_bundle(
        original.model_copy(
            update={
                "collected_at": renewed_at,
                "records": (
                    original.records[0].model_copy(update={"fetched_at": renewed_at}),
                ),
            }
        )
    )
    write_json(paths["sources"], renewed.model_dump(mode="json"))
    approved = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用 方案 B。"),
    )
    write_json(
        paths["context"].with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    write_json(paths["state"], semantic_state(renewed, approved))
    run_token = start_pending_run()

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_reused"
    written = QwenProjectContextArtifact.model_validate_json(
        paths["context"].read_bytes()
    )
    assert written.context == approved.context.model_copy(
        update={"generated_at": renewed_at}
    )
    assert written.completed_retrieval_request_ids == ()
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="artifact",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_requires_skill_when_source_changes(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(paths["state"], semantic_state(original, approved))
    changed = rehash_bundle(
        original.model_copy(
            update={
                "records": (
                    original.records[0].model_copy(update={"source_version": "v2"}),
                )
            }
        )
    )
    write_json(paths["sources"], changed.model_dump(mode="json"))
    before = paths["context"].read_bytes()
    run_token = start_pending_run()

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_required"
    assert paths["context"].read_bytes() == before
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_requires_skill_for_legacy_state(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    write_json(
        paths["state"],
        {
            "schema_version": 1,
            "project_id": "project-1",
            "last_cursor": 1,
            "last_content_hash": "a" * 64,
            "last_sync_id": "sync-1",
            "pending": None,
        },
    )
    before = paths["context"].read_bytes()
    run_token = start_pending_run()

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_required"
    assert paths["context"].read_bytes() == before
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_fails_closed_when_approved_is_missing(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(paths["state"], semantic_state(selected, approved))
    before = paths["context"].read_bytes()
    run_token = start_pending_run()

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "approved_artifact_unavailable",
    }
    assert paths["context"].read_bytes() == before
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_rolls_back_when_stage_commit_fails(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(
        paths["context"].with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    write_json(paths["state"], semantic_state(selected, approved))
    before = paths["context"].read_bytes()
    run_token = start_pending_run()
    original_write_state = lifecycle._write_state

    def fail_artifact_stage(path: Path, payload: dict[str, object]) -> None:
        if payload["stage"] == "artifact":
            raise ValueError("private_file_write_failed")
        original_write_state(path, payload)

    monkeypatch.setattr(lifecycle, "_write_state", fail_artifact_stage)

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "private_file_write_failed",
    }
    assert paths["context"].read_bytes() == before
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_rejects_conflicting_pending_identity(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(
        paths["context"].with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    conflicting_hash = "b" * 64
    write_json(
        paths["state"],
        semantic_state(
            selected,
            approved,
            pending={
                "source_cursor": 2,
                "content_hash": conflicting_hash,
                "sync_id": sync_cli._sync_id(
                    "project-1", 2, conflicting_hash
                ),
                "completion_claims_hash": digest("[]"),
            },
        ),
    )
    before = paths["context"].read_bytes()
    run_token = start_pending_run()

    assert main(reuse_artifact_args(paths, run_token), now=lambda: NOW) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_sync_conflict",
    }
    assert paths["context"].read_bytes() == before
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_reuse_artifact_rejects_non_pending_lifecycle_stage(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    started = lifecycle.begin_run(
        "project-1",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )
    assert started.run_token is not None

    assert main(
        reuse_artifact_args(paths, started.run_token), now=lambda: NOW
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "run_stage_invalid",
    }
    lifecycle.assert_stage(
        "project-1",
        started.run_token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_artifact_fails_closed_when_unchanged_approved_is_missing(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(paths["state"], semantic_state(selected, approved))
    before = paths["context"].read_bytes()
    run_token = start_pending_run()

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(approved.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "approved_artifact_unavailable",
    }
    assert paths["context"].read_bytes() == before


def test_artifact_uses_candidate_when_source_semantics_change(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用 方案 B。"),
    )
    write_json(
        paths["context"].with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    write_json(paths["state"], semantic_state(original, approved))
    changed = rehash_bundle(
        original.model_copy(
            update={
                "records": (
                    original.records[0].model_copy(update={"source_version": "v2"}),
                )
            }
        )
    )
    write_json(paths["sources"], changed.model_dump(mode="json"))
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="方案 B。"),
    )

    assert main(
        artifact_args(paths, start_pending_run()),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_written"
    written = QwenProjectContextArtifact.model_validate_json(
        paths["context"].read_bytes()
    )
    assert written.context.source_refs[0].excerpt == "方案 B。"


def test_artifact_uses_candidate_for_legacy_state_without_approved(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    write_json(
        paths["state"],
        {
            "schema_version": 1,
            "project_id": "project-1",
            "last_cursor": 1,
            "last_content_hash": "a" * 64,
            "last_sync_id": "sync-1",
            "pending": None,
        },
    )
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="方案 B。"),
    )

    assert main(
        artifact_args(paths, start_pending_run()),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "artifact_written"
    assert QwenProjectContextArtifact.model_validate_json(
        paths["context"].read_bytes()
    ).context.source_refs[0].excerpt == "方案 B。"


@pytest.mark.parametrize("matches", [True, False])
def test_artifact_pending_requires_exact_reconstruction(
    tmp_path: Path,
    capsys,
    matches: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    approved = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用 方案 B。"),
    )
    write_json(
        paths["context"].with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    pending_context = approved.context if matches else context(excerpt="方案 B。")
    pending_envelope = sync_cli._build_envelope(
        project(),
        selected,
        pending_context,
        completed_retrieval_request_ids=(),
        source_cursor=2,
        now=NOW,
    )
    pending = {
        "source_cursor": 2,
        "content_hash": pending_envelope.content_hash,
        "sync_id": pending_envelope.sync_id,
        "completion_claims_hash": digest("[]"),
    }
    write_json(paths["state"], semantic_state(selected, approved, pending=pending))
    before = paths["context"].read_bytes()

    result = main(
        artifact_args(paths, start_pending_run()),
        input_stream=io.BytesIO(
            canonical(approved.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    )
    output = json.loads(capsys.readouterr().out)
    if matches:
        assert result == 0
        assert output["status"] == "artifact_written"
    else:
        assert result == 1
        assert output["error_type"] == "pending_sync_conflict"
        assert paths["context"].read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"{}"],
)
def test_approved_reader_rejects_invalid_content(
    tmp_path: Path,
    payload: bytes,
) -> None:
    context_path = tmp_path / "context.json"
    approved_path = context_path.with_name("context.approved.json")
    approved_path.write_bytes(payload)
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, "a" * 64)


def test_approved_reader_rejects_oversized_content(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    approved_path = context_path.with_name("context.approved.json")
    approved_path.write_bytes(b"x" * (sync_cli.MAX_PRIVATE_INPUT_BYTES + 1))
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, "a" * 64)


def test_approved_reader_rejects_hash_mismatch(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(
        context_path.with_name("context.approved.json"),
        approved.model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, "a" * 64)


def test_approved_reader_rejects_directory(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.with_name("context.approved.json").mkdir()
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, "a" * 64)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_approved_reader_rejects_aliases(tmp_path: Path, kind: str) -> None:
    context_path = tmp_path / "context.json"
    approved_path = context_path.with_name("context.approved.json")
    ordinary_context = tmp_path / "ordinary.json"
    target = ordinary_context.with_name("ordinary.approved.json")
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(target, approved.model_dump(mode="json"))
    expected_hash = artifact_hash(approved)
    assert sync_cli._read_approved_artifact(
        ordinary_context,
        expected_hash,
    ) == approved
    try:
        if kind == "symlink":
            approved_path.symlink_to(target)
        else:
            sync_cli.os.link(target, approved_path)
    except OSError as exc:
        pytest.skip(f"{kind} unavailable: {exc}")
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, expected_hash)


def test_approved_reader_rejects_hardlink_added_during_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context_path = tmp_path / "context.json"
    approved_path = context_path.with_name("context.approved.json")
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(approved_path, approved.model_dump(mode="json"))
    expected_hash = artifact_hash(approved)
    assert sync_cli._read_approved_artifact(context_path, expected_hash) == approved
    alias = tmp_path / "late-alias.json"
    path_type = type(approved_path)
    real_lstat = path_type.lstat
    calls = 0

    def add_link_before_final_lstat(self):  # type: ignore[no-untyped-def]
        nonlocal calls
        if self == approved_path:
            calls += 1
            if calls == 2:
                try:
                    sync_cli.os.link(approved_path, alias)
                except OSError as exc:
                    pytest.skip(f"hardlinks unavailable: {exc}")
        return real_lstat(self)

    monkeypatch.setattr(path_type, "lstat", add_link_before_final_lstat)
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, expected_hash)


@pytest.mark.parametrize("command", ["artifact", "push"])
def test_cli_rejects_derived_approved_state_overlap_before_effects(
    tmp_path: Path,
    capsys,
    command: str,
) -> None:
    paths = write_push_inputs(tmp_path)
    paths["state"] = paths["context"].with_name("context.approved.json")
    before = {key: path.read_bytes() for key, path in paths.items() if path.exists()}
    if command == "artifact":
        argv = artifact_args(paths, start_pending_run())
        kwargs = {
            "input_stream": io.BytesIO(paths["context"].read_bytes()),
            "now": lambda: NOW,
        }
    else:
        argv = push_args(paths)
        kwargs = {
            "urlopen": lambda *_a, **_k: pytest.fail("overlap must precede network"),
            "environ": {"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        }

    assert main(argv, **kwargs) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == (
        "private_paths_overlap"
    )
    assert {key: path.read_bytes() for key, path in paths.items() if path.exists()} == (
        before
    )


def test_cli_rejects_derived_approved_samefile_alias_before_artifact_write(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    approved_path = paths["context"].with_name("context.approved.json")
    try:
        sync_cli.os.link(paths["sources"], approved_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    before_sources = paths["sources"].read_bytes()
    before_context = paths["context"].read_bytes()

    assert main(
        artifact_args(paths, start_pending_run()),
        input_stream=io.BytesIO(before_context),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == (
        "private_paths_overlap"
    )
    assert paths["sources"].read_bytes() == before_sources
    assert paths["context"].read_bytes() == before_context


def test_approved_reader_rejects_final_lstat_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context_path = tmp_path / "context.json"
    approved_path = context_path.with_name("context.approved.json")
    approved = QwenProjectContextArtifact(schema_version=1, context=context())
    write_json(approved_path, approved.model_dump(mode="json"))
    expected_hash = artifact_hash(approved)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"replacement")
    path_type = type(approved_path)
    real_lstat = path_type.lstat
    calls = 0

    def raced_lstat(self):  # type: ignore[no-untyped-def]
        nonlocal calls
        if self == approved_path:
            calls += 1
            if calls == 2:
                return real_lstat(replacement)
        return real_lstat(self)

    monkeypatch.setattr(path_type, "lstat", raced_lstat)
    with pytest.raises(ValueError, match="approved_artifact_unavailable"):
        sync_cli._read_approved_artifact(context_path, expected_hash)


def collect_args(paths: dict[str, Path], *extra: str) -> list[str]:
    return [
        "collect",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--dws-path",
        str(paths["manifest"]),
        "--output",
        str(paths["sources"]),
        *extra,
    ]


def host_import_payload(
    *,
    project_id: str = "project-1",
    markdown: str = "# 决策\n采用方案 B。",
    info: object | None = None,
) -> bytes:
    payloads = (
        (
            "doc_info",
            info or {
                "result": {
                    "nodeId": "doc-1",
                    "contentType": "ALIDOC",
                    "extension": "adoc",
                    "title": "决策文档",
                    "shareUrl": "dingtalk://document/doc-1",
                    "version": "v1",
                    "updatedAt": NOW.isoformat(),
                }
            },
        ),
        ("doc_read", {"data": {"markdown": markdown}}),
    )
    results = []
    for operation, value in payloads:
        raw = canonical(value).encode("utf-8")
        results.append(
            {
                "operation": operation,
                "encoding": "base64-json",
                "byte_count": len(raw),
                "payload": base64.b64encode(raw).decode("ascii"),
            }
        )
    return canonical(
        {
            "schema_version": 1,
            "project_id": project_id,
            "results": results,
        }
    ).encode("utf-8")


def host_import_args(paths: dict[str, Path], token: str) -> list[str]:
    return [
        "host-import",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--output",
        str(paths["sources"]),
        "--run-token",
        token,
    ]


class FakeDws:
    def run(self, args: tuple[str, ...]) -> dict[str, object]:
        if args[:2] == ("doc", "info"):
            return {
                "nodeId": "doc-1",
                "contentType": "ALIDOC",
                "extension": "adoc",
                "title": "决策文档",
                "shareUrl": "dingtalk://document/doc-1",
                "version": "v1",
                "updatedAt": NOW.isoformat(),
            }
        if args[:2] == ("doc", "read"):
            return {"markdown": "# 决策\n采用方案 B。"}
        if args[:3] == ("minutes", "get", "info"):
            return {"taskUuid": "meeting-1", "title": "评审会"}
        if args[:3] == ("minutes", "get", "summary"):
            return {"markdown": "采用方案 B。"}
        if args[:3] == ("minutes", "get", "transcription"):
            return {"paragraphs": [{"text": "采用方案 B。"}], "nextToken": ""}
        if args[:3] == ("minutes", "get", "todos"):
            return {"todos": []}
        if args[:3] == ("todo", "task", "get"):
            return {"taskId": "task-1", "subject": "采用方案 B。"}
        if args[:3] == ("calendar", "event", "list"):
            return {"events": [{"eventId": "event-1"}]}
        if args[:3] == ("calendar", "event", "get"):
            return {"eventId": "event-1", "summary": "采用方案 B。"}
        raise AssertionError(args)


class RecordingUrlOpen:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.request = None
        self.timeout = None
        self.response = response
        self.read_sizes: list[int | None] = []

    def __call__(self, request, *, timeout: float):  # type: ignore[no-untyped-def]
        self.request = request
        self.timeout = timeout
        request_payload = json.loads(request.data)
        response = self.response or {
            "sync_id": request_payload["sync_id"],
            "outcome": "applied",
            "project_status": "healthy",
            "accepted_sources": len(request_payload["sources"])
            + len(request_payload["tombstones"]),
            "failed_sources": 0,
            "generation_id": "generation-private",
            "next_sync_before": "2026-09-05T12:05:00+00:00",
        }

        def read(size: int | None = None) -> bytes:
            self.read_sizes.append(size)
            return canonical(response).encode("utf-8")

        return SimpleNamespace(
            read=read,
            __enter__=lambda self: self,
            __exit__=lambda *_args: None,
        )


def _concurrent_push_worker(
    args: list[str],
    start_barrier: object,
    load_barrier: object,
    observations: object,
) -> None:
    lock_root = (
        Path(args[args.index("--state-file") + 1]).parent / "dws-sync-locks"
    )
    sync_cli.LIFECYCLE_ROOT = lock_root
    state_lock.PRIVATE_LOCK_ROOT = lock_root
    original_load_state = sync_cli._load_state

    def synchronized_load_state(
        path: Path, project_id: str
    ):  # type: ignore[no-untyped-def]
        state = original_load_state(path, project_id)
        observations.put(  # type: ignore[attr-defined]
            ("loaded", state.last_cursor, state.pending is None)
        )
        try:
            load_barrier.wait(timeout=5)  # type: ignore[attr-defined]
        except BrokenBarrierError:
            pass
        return state

    def urlopen(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 30.0
        request_payload = json.loads(request.data)
        observations.put(  # type: ignore[attr-defined]
            ("sent", request_payload["source_cursor"], request_payload["sync_id"])
        )
        response = {
            "sync_id": request_payload["sync_id"],
            "outcome": "applied",
            "project_status": "healthy",
            "accepted_sources": 1,
            "failed_sources": 0,
            "generation_id": "generation-private",
            "next_sync_before": "2026-09-05T12:05:00+00:00",
        }
        return SimpleNamespace(
            read=lambda _size: canonical(response).encode("utf-8"),
            close=lambda: None,
        )

    start_barrier.wait(timeout=5)  # type: ignore[attr-defined]
    sync_cli._load_state = synchronized_load_state
    result = main(
        args,
        urlopen=urlopen,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    )
    observations.put(("result", result))  # type: ignore[attr-defined]


def _blocking_push_worker(
    args: list[str],
    entered_http: object,
    release_http: object,
    observations: object,
) -> None:
    lock_root = (
        Path(args[args.index("--state-file") + 1]).parent / "dws-sync-locks"
    )
    sync_cli.LIFECYCLE_ROOT = lock_root
    state_lock.PRIVATE_LOCK_ROOT = lock_root
    def urlopen(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 30.0
        request_payload = json.loads(request.data)
        observations.put(  # type: ignore[attr-defined]
            ("sent", request_payload["source_cursor"], request_payload["sync_id"])
        )
        entered_http.set()  # type: ignore[attr-defined]
        if not release_http.wait(timeout=5):  # type: ignore[attr-defined]
            raise TimeoutError
        response = {
            "sync_id": request_payload["sync_id"],
            "outcome": "applied",
            "project_status": "healthy",
            "accepted_sources": 1,
            "failed_sources": 0,
            "generation_id": "generation-private",
            "next_sync_before": "2026-09-05T12:05:00+00:00",
        }
        return SimpleNamespace(
            read=lambda _size: canonical(response).encode("utf-8"),
            close=lambda: None,
        )

    result = main(
        args,
        urlopen=urlopen,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    )
    observations.put(("result", result))  # type: ignore[attr-defined]


class PendingUrlOpen:
    def __init__(self, source_hash: str) -> None:
        self.source_hash = source_hash
        self.request = None

    def __call__(self, request, *, timeout: float):  # type: ignore[no-untyped-def]
        self.request = request
        assert timeout == 30.0
        payload = {
            "requests": [
                {
                    "request_id": "retrieval-1",
                    "project_id": "project-1",
                    "query_hash": digest("missing detail"),
                    "source_id_hashes": [self.source_hash],
                    "baseline_generation_id": "generation-1",
                    "baseline_content_hash": "a" * 64,
                    "baseline_source_cursor": 1,
                    "baseline_sources": [
                        {
                            "source_id_hash": self.source_hash,
                            "source_version": "v1",
                            "content_hash": "b" * 64,
                            "chunk_fingerprint": "c" * 64,
                        }
                    ],
                    "status": "in_progress",
                    "created_at": NOW.isoformat(),
                    "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
                    "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                    "attempt_count": 1,
                    "request_epoch": 2,
                    "lease_token": "t" * 43,
                    "completed_at": None,
                }
            ]
        }
        return SimpleNamespace(
            read=lambda size: canonical(payload).encode("utf-8"),
            close=lambda: None,
        )


def test_pending_fetch_maps_gateway_hashes_into_private_source_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = PendingUrlOpen(digest("doc-1"))

    assert main(
        pending_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0

    output = json.loads(capsys.readouterr().out)
    private = json.loads(paths["sources"].read_text(encoding="utf-8"))
    request = private["retrieval_requests"][0]
    assert sent.request.full_url.endswith(
        "/v1/projects/project-1/retrieval-requests?status=pending"
    )
    assert sent.request.method == "GET"
    assert sent.request.get_header("Authorization") == "Bearer private-token"
    assert request == {
        "request_id": "retrieval-1",
        "query_hash": digest("missing detail"),
        "request_epoch": 2,
        "attempt_count": 1,
        "lease_expires_at": "2026-09-05T12:05:00Z",
        "lease_token": "t" * 43,
        "sources": [{"source_id": "doc-1", "source_type": "document"}],
    }
    assert output == {
        "status": "pending_fetched",
        "project_id": "project-1",
        "request_count": 1,
        "source_count": 1,
        "content_hash": private["content_hash"],
    }
    assert "private-token" not in canonical(output)


def test_pending_fetch_rejects_unmapped_source_without_replacing_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["sources"].read_bytes()

    assert main(
        pending_args(paths),
        urlopen=PendingUrlOpen("f" * 64),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "retrieval_request_invalid",
    }
    assert paths["sources"].read_bytes() == original


def test_push_completes_only_requests_present_in_source_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    assert main(
        pending_args(paths),
        urlopen=PendingUrlOpen(digest("doc-1")),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    claimed = QwenProjectContextArtifact(
        schema_version=1,
        context=context(),
        completed_retrieval_request_ids=("retrieval-1",),
    )
    write_json(paths["context"], claimed.model_dump(mode="json"))

    sent = RecordingUrlOpen()
    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    payload = json.loads(sent.request.data)
    assert payload["completed_retrieval_request_ids"] == []
    assert payload["completed_retrieval_claims"] == [
        {
            "request_id": "retrieval-1",
            "request_epoch": 2,
            "attempt_count": 1,
            "lease_token": "t" * 43,
        }
    ]

    unclaimed = claimed.model_copy(
        update={"completed_retrieval_request_ids": ("retrieval-other",)}
    )
    write_json(paths["context"], unclaimed.model_dump(mode="json"))
    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("invalid request must not use network"),
        environ={},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "retrieval_request_invalid",
    }


def test_production_lifecycle_fences_every_mutating_stage(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    paths = write_push_inputs(tmp_path)
    monkeypatch.setattr(sync_cli, "LIFECYCLE_ROOT", tmp_path / "locks")

    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    begun = json.loads(capsys.readouterr().out)
    token = begun["run_token"]
    assert begun["status"] == "started"

    assert main(
        collect_args(paths, "--run-token", token),
        runner=FakeDws(),
        now=lambda: NOW,
    ) == 0
    capsys.readouterr()
    pending = pending_args(paths) + ["--run-token", token]
    assert main(
        pending,
        urlopen=PendingUrlOpen(digest("doc-1")),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        now=lambda: NOW,
    ) == 0
    capsys.readouterr()

    artifact = QwenProjectContextArtifact(
        schema_version=1,
        context=context(),
        completed_retrieval_request_ids=("retrieval-1",),
    )
    artifact_command = artifact_args(paths, token)
    assert main(
        artifact_command,
        input_stream=io.BytesIO(
            canonical(artifact.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 0
    capsys.readouterr()
    assert main(
        push_args(paths, "--run-token", token),
        urlopen=RecordingUrlOpen(),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        now=lambda: NOW,
    ) == 0
    capsys.readouterr()
    assert main(
        ["end", "--project", "project-1", "--run-token", token],
        now=lambda: NOW,
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "project_id": "project-1",
        "run_token": None,
    }


def test_expired_run_cannot_replace_artifact(tmp_path: Path, capsys, monkeypatch) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["context"].read_bytes()
    monkeypatch.setattr(sync_cli, "LIFECYCLE_ROOT", tmp_path / "locks")
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    old_token = json.loads(capsys.readouterr().out)["run_token"]
    assert main(
        ["begin", "--project", "project-1"],
        now=lambda: NOW + timedelta(hours=1),
    ) == 0
    capsys.readouterr()

    assert main(
        artifact_args(paths, old_token),
        input_stream=io.BytesIO(original),
        now=lambda: NOW + timedelta(hours=1),
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "run_token_invalid"
    assert paths["context"].read_bytes() == original


def test_artifact_rejects_heading_excerpt_before_write_or_stage(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["context"].read_bytes()
    title_markdown = "# 决策\n采用方案 B"
    write_json(
        paths["sources"],
        bundle(active_record(content=title_markdown)).model_dump(mode="json"),
    )
    run_token = start_pending_run()
    artifact = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt=title_markdown),
    )

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(artifact.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "source_excerpt_mismatch",
    }
    assert paths["context"].read_bytes() == original
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    ("updates", "error_type"),
    [
        ({"project_name": "其他项目"}, "context_mismatch"),
        (
            {"permission_scope": "project:other", "source_refs": ()},
            "context_mismatch",
        ),
        (
            {"generated_at": NOW + timedelta(seconds=1)},
            "context_collection_mismatch",
        ),
    ],
)
def test_artifact_validates_context_against_manifest_and_collection(
    tmp_path: Path,
    capsys,
    updates: dict[str, object],
    error_type: str,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["context"].read_bytes()
    run_token = start_pending_run()
    artifact = QwenProjectContextArtifact(
        schema_version=1,
        context=context().model_copy(update=updates),
    )

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(artifact.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": error_type,
    }
    assert paths["context"].read_bytes() == original
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_artifact_validates_source_bundle_before_write_or_stage(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["context"].read_bytes()
    source_payload = json.loads(paths["sources"].read_text(encoding="utf-8"))
    source_payload["content_hash"] = "0" * 64
    write_json(paths["sources"], source_payload)
    run_token = start_pending_run()
    artifact = QwenProjectContextArtifact(schema_version=1, context=context())

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(artifact.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "source_bundle_hash_mismatch",
    }
    assert paths["context"].read_bytes() == original
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_artifact_restores_previous_file_when_stage_commit_interrupts(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["context"].unlink()
    original_artifact = paths["context"].read_bytes() if existing else None
    run_token = start_pending_run()
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用方案 B。"),
    )
    original_write_state = lifecycle._write_state

    interrupted = False

    def commit_then_interrupt(path: Path, payload: dict[str, object]) -> None:
        nonlocal interrupted
        original_write_state(path, payload)
        if payload["stage"] == "artifact":
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "_write_state", commit_then_interrupt)

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }
    assert (
        paths["context"].read_bytes() if paths["context"].exists() else None
    ) == original_artifact
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (KeyboardInterrupt, "interrupted"),
        (RuntimeError, "sync_failed"),
    ],
)
def test_artifact_restores_output_when_apply_fails_after_replace(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
    failure: type[BaseException],
    error_type: str,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["context"].unlink()
    original_artifact = paths["context"].read_bytes() if existing else None
    run_token = start_pending_run()
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用方案 B。"),
    )
    real_apply = sync_cli._RecoverableAtomicWrite.apply

    def fail_after_replace(operation) -> None:  # type: ignore[no-untyped-def]
        real_apply(operation)
        raise failure("private-apply-detail")

    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "apply",
        fail_after_replace,
    )

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    public = capsys.readouterr().out
    assert json.loads(public) == {"status": "error", "error_type": error_type}
    assert "private-apply-detail" not in public
    assert (
        paths["context"].read_bytes() if paths["context"].exists() else None
    ) == original_artifact
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_artifact_rollback_failure_is_sanitized_after_restoration(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    original_artifact = paths["context"].read_bytes()
    run_token = start_pending_run()
    candidate = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用方案 B。"),
    )
    original_write_state = lifecycle._write_state
    real_rollback = sync_cli._RecoverableAtomicWrite.rollback
    rollback_calls = 0

    def fail_artifact_stage(path: Path, payload: dict[str, object]) -> None:
        if payload["stage"] == "artifact":
            raise ValueError("private_file_write_failed")
        original_write_state(path, payload)

    def restore_then_fail(operation) -> None:  # type: ignore[no-untyped-def]
        nonlocal rollback_calls
        rollback_calls += 1
        real_rollback(operation)
        raise RuntimeError("private-rollback-detail")

    monkeypatch.setattr(lifecycle, "_write_state", fail_artifact_stage)
    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "rollback",
        restore_then_fail,
    )

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(
            canonical(candidate.model_dump(mode="json")).encode("utf-8")
        ),
        now=lambda: NOW,
    ) == 1
    public = capsys.readouterr().out
    assert json.loads(public) == {
        "status": "error",
        "error_type": "private_file_write_failed",
    }
    assert "private-rollback-detail" not in public
    assert rollback_calls == 1
    assert paths["context"].read_bytes() == original_artifact
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_artifact_rejects_canonical_expansion_before_transaction(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    paths["context"].unlink()
    run_token = start_pending_run()
    payload = {
        "schema_version": 1,
        "context": {
            "project_id": "project-1",
            "project_name": "测试项目",
            "generated_at": NOW.isoformat(),
            "permission_scope": SCOPE,
        },
    }
    raw = canonical(payload).encode("utf-8")
    expanded = canonical(
        QwenProjectContextArtifact.model_validate(payload).model_dump(mode="json")
    ).encode("utf-8")
    assert len(raw) < len(expanded)
    encoded_limit = len(raw)
    real_validate_bundle = sync_cli._validate_bundle

    def validate_then_lower_limit(selected, source_bundle) -> None:  # type: ignore[no-untyped-def]
        real_validate_bundle(selected, source_bundle)
        monkeypatch.setattr(sync_cli, "MAX_PRIVATE_INPUT_BYTES", encoded_limit)

    monkeypatch.setattr(sync_cli, "_validate_bundle", validate_then_lower_limit)
    monkeypatch.setattr(
        sync_cli,
        "_RecoverableAtomicWrite",
        lambda *_args: pytest.fail("oversized output must fail before transaction"),
    )

    assert main(
        artifact_args(paths, run_token),
        input_stream=io.BytesIO(raw),
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "context_file_too_large",
    }
    assert not paths["context"].exists()
    lifecycle.assert_stage(
        "project-1",
        run_token,
        expected="pending",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_active_lifecycle_blocks_all_no_token_commands_before_side_effects(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    paths = write_push_inputs(tmp_path)
    original_sources = paths["sources"].read_bytes()
    monkeypatch.setattr(sync_cli, "LIFECYCLE_ROOT", tmp_path / "locks")
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    capsys.readouterr()

    class ForbiddenRunner:
        def run(self, _args):  # type: ignore[no-untyped-def]
            pytest.fail("active lifecycle must block DWS reads")

    assert main(
        collect_args(paths), runner=ForbiddenRunner(), now=lambda: NOW
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "lifecycle_active"
    assert paths["sources"].read_bytes() == original_sources

    assert main(
        pending_args(paths),
        urlopen=lambda *_a, **_k: pytest.fail(
            "active lifecycle must block retrieval claim"
        ),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "lifecycle_active"
    assert paths["sources"].read_bytes() == original_sources

    assert main(
        push_args(paths),
        urlopen=lambda *_a, **_k: pytest.fail("active lifecycle must block push"),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        now=lambda: NOW,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "lifecycle_active"
    assert not paths["state"].exists()


def test_gateway_retries_transient_transport_with_same_request_and_closes(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    requests: list[object] = []
    closed: list[bool] = []

    def transient_then_success(request, *, timeout):  # type: ignore[no-untyped-def]
        requests.append(request)
        if len(requests) < 3:
            raise URLError("private")
        response = RecordingUrlOpen()(request, timeout=timeout)
        response.close = lambda: closed.append(True)
        return response

    assert main(
        push_args(paths),
        urlopen=transient_then_success,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 0
    capsys.readouterr()
    assert len(requests) == 3
    assert requests[0] is requests[1] is requests[2]
    assert closed == [True]


@pytest.mark.parametrize("status", [302, 401])
def test_gateway_does_not_retry_redirect_or_authentication_error(
    tmp_path: Path, capsys, status: int
) -> None:
    paths = write_push_inputs(tmp_path)
    attempts = 0

    def rejected(request, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, status, "private", {}, None)

    assert main(
        push_args(paths),
        urlopen=rejected,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "http_error"
    assert attempts == 1


def test_gateway_exposes_allowlisted_http_error_detail_and_closes_body(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    body = io.BytesIO(b'{"detail":"clock_skew_exceeded"}')
    closed = False

    def rejected(request, *, timeout):  # type: ignore[no-untyped-def]
        error = HTTPError(request.full_url, 400, "private", {}, body)
        original_close = error.close

        def close() -> None:
            nonlocal closed
            closed = True
            original_close()

        error.close = close
        raise error

    assert main(
        push_args(paths),
        urlopen=rejected,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == (
        "clock_skew_exceeded"
    )
    assert closed is True


@pytest.mark.parametrize(
    "body",
    [
        b'{"detail":"private database row 42"}',
        b'{"detail":"clock_skew_exceeded","private":"secret"}',
        (
            b'{"detail":"private database row 42",'
            b'"detail":"clock_skew_exceeded"}'
        ),
        b'{"detail":',
        b"x" * 65_537,
        b" " * (65_537 - len(b'{"detail":"clock_skew_exceeded"}'))
        + b'{"detail":"clock_skew_exceeded"}',
    ],
    ids=(
        "unknown",
        "extra-key",
        "duplicate-key",
        "invalid-json",
        "oversized",
        "oversized-valid-json",
    ),
)
def test_gateway_hides_unsafe_http_error_bodies(
    tmp_path: Path, capsys, body: bytes
) -> None:
    paths = write_push_inputs(tmp_path)
    private_detail = "private database row 42"
    response_body = io.BytesIO(body)
    closed = False

    def rejected(request, *, timeout):  # type: ignore[no-untyped-def]
        error = HTTPError(request.full_url, 409, "private", {}, response_body)
        original_close = error.close

        def close() -> None:
            nonlocal closed
            closed = True
            original_close()

        error.close = close
        raise error

    assert main(
        push_args(paths),
        urlopen=rejected,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 1
    public = capsys.readouterr().out
    assert json.loads(public)["error_type"] == "http_error"
    assert private_detail not in public
    assert closed is True


def test_gateway_http_error_fallback_read_uses_remaining_timeout() -> None:
    timeouts: list[float] = []
    read_sizes: list[int] = []
    raw = b'{"detail":"clock_skew_exceeded"}'

    def read(size: int) -> bytes:
        read_sizes.append(size)
        return raw

    body = SimpleNamespace(
        read=read,
        settimeout=lambda timeout: timeouts.append(timeout),
        close=lambda: None,
    )
    error = HTTPError("http://127.0.0.1:8731", 400, "private", {}, body)
    ticks = iter((10.0, 10.5))

    assert sync_cli._safe_gateway_error_type(
        error,
        deadline=15.0,
        monotonic=lambda: next(ticks),
    ) == "clock_skew_exceeded"
    assert timeouts == [5.0]
    assert read_sizes == [65_537]


def test_gateway_http_error_fallback_does_not_read_after_deadline() -> None:
    read_sizes: list[int] = []
    body = SimpleNamespace(
        read=lambda size: read_sizes.append(size) or b"{}",
        settimeout=lambda _timeout: pytest.fail("deadline must fail first"),
        close=lambda: None,
    )
    error = HTTPError("http://127.0.0.1:8731", 400, "private", {}, body)

    assert sync_cli._safe_gateway_error_type(
        error,
        deadline=10.0,
        monotonic=lambda: 10.0,
    ) == "http_error"
    assert read_sizes == []


def test_gateway_retries_retryable_http_error_without_exposing_body(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    attempts = 0
    closed = 0
    private_detail = "private retry detail"

    def rejected(request, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        error = HTTPError(
            request.full_url,
            503,
            "private",
            {},
            io.BytesIO(canonical({"detail": private_detail}).encode("utf-8")),
        )
        original_close = error.close

        def close() -> None:
            nonlocal closed
            closed += 1
            original_close()

        error.close = close
        raise error

    assert main(
        push_args(paths),
        urlopen=rejected,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 1
    public = capsys.readouterr().out
    assert json.loads(public)["error_type"] == "http_error"
    assert private_detail not in public
    assert attempts == 3
    assert closed == 3


def test_gateway_invalid_response_is_not_retried_and_is_closed(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    attempts = 0
    closed = 0

    def invalid(_request, *, timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1

        def close() -> None:
            nonlocal closed
            closed += 1

        return SimpleNamespace(read=lambda _size: b"{}", close=close)

    assert main(
        push_args(paths),
        urlopen=invalid,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        sleep=lambda _delay: None,
    ) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "response_invalid"
    assert attempts == 1
    assert closed == 1


def test_gateway_rejects_response_that_finishes_after_total_deadline() -> None:
    response = SimpleNamespace(read=lambda _size: b"{}", close=lambda: None)
    clock = iter((0.0, 31.0))

    with pytest.raises(ValueError, match="^network_timeout$"):
        sync_cli._gateway_request(
            Request("http://127.0.0.1:8731/test"),
            opener=lambda _request, *, timeout: response,
            parse=lambda raw: raw,
            monotonic=lambda: next(clock),
            sleep=lambda _delay: None,
        )


def test_collect_never_prints_business_content(tmp_path: Path, capsys) -> None:
    selected = project(
        sources=(
            source_spec("document", "doc-1"),
            source_spec("meeting_note", "meeting-1"),
            source_spec("task", "task-1"),
            source_spec("calendar", "event-1"),
        )
    )
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "private-bundle.json"
    dws_path = tmp_path / "dws.exe"
    dws_path.write_bytes(b"")
    write_manifest(manifest, selected)

    result = main(
        [
            "collect",
            "--manifest",
            str(manifest),
            "--project",
            "project-1",
            "--dws-path",
            str(dws_path),
            "--output",
            str(output),
        ],
        runner=FakeDws(),
        urlopen=lambda *_a, **_k: pytest.fail("collect must not use network"),
        now=lambda: NOW,
    )

    public = json.loads(capsys.readouterr().out)
    private = output.read_bytes()
    assert result == 0
    assert public == {
        "active_sources": 4,
        "content_hash": json.loads(private)["content_hash"],
        "failed_sources": 0,
        "output_bytes": len(private),
        "project_id": "project-1",
        "source_count": 4,
        "status": "collected",
    }
    assert private == canonical(json.loads(private)).encode("utf-8")
    assert "采用方案" not in canonical(public)
    assert "private-profile" not in canonical(public)


def test_collect_rejects_oversized_bundle_before_atomic_write(
    tmp_path: Path,
    capsys,
) -> None:
    selected = project()
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "private-bundle.json"
    output.write_bytes(b"existing-private-state")
    dws_path = tmp_path / "dws.exe"
    dws_path.write_bytes(b"")
    write_manifest(manifest, selected)

    class OversizedDws(FakeDws):
        def run(self, args: tuple[str, ...]) -> dict[str, object]:
            if args[:2] == ("doc", "read"):
                return {"markdown": "采用方案 B。" + "x" * 1_100_000}
            return super().run(args)

    assert main(
        [
            "collect",
            "--manifest",
            str(manifest),
            "--project",
            "project-1",
            "--dws-path",
            str(dws_path),
            "--output",
            str(output),
        ],
        runner=OversizedDws(),
        urlopen=lambda *_a, **_k: pytest.fail("collect must not use network"),
        now=lambda: NOW,
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "sources_file_too_large",
    }
    assert output.read_bytes() == b"existing-private-state"


def test_host_import_writes_active_bundle_and_sanitized_stdout(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    paths["sources"].unlink()
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    private_markdown = '# 决策\n采用方案 B，包含中文与 "引号"。'

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload(markdown=private_markdown)),
    ) == 0

    public = json.loads(capsys.readouterr().out)
    assert public == {
        "status": "collected",
        "project_id": "project-1",
        "source_count": 1,
        "active_sources": 1,
        "failed_sources": 0,
        "content_hash": public["content_hash"],
        "output_bytes": paths["sources"].stat().st_size,
    }
    output = canonical(public)
    for secret in (
        private_markdown,
        "private-profile",
        "doc-1",
        token,
    ):
        assert secret not in output
    written = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    assert written.records[0].content_text == private_markdown
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="collected",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_host_import_failure_preserves_output_and_begun_stage(
    tmp_path: Path,
    capsys,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["sources"].unlink()
    original = paths["sources"].read_bytes() if existing else None
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload(project_id="wrong")),
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "host_import_invalid",
    }
    assert (
        paths["sources"].read_bytes() if paths["sources"].exists() else None
    ) == original
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_host_import_checks_stage_before_reading_stdin(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)

    class ForbiddenInput:
        def read(self, *_args):  # type: ignore[no-untyped-def]
            pytest.fail("stdin must not be read before lifecycle validation")

    assert main(
        host_import_args(paths, "stale-token"),
        now=lambda: NOW,
        input_stream=ForbiddenInput(),
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "run_token_invalid",
    }


def test_host_import_normalizes_input_read_failure(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["sources"].read_bytes()
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]

    class FailedInput:
        def read(self, *_args):  # type: ignore[no-untyped-def]
            raise OSError("private-input-detail")

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=FailedInput(),
    ) == 1
    public = capsys.readouterr().out
    assert json.loads(public) == {
        "status": "error",
        "error_type": "host_import_invalid",
    }
    assert "private-input-detail" not in public
    assert paths["sources"].read_bytes() == original


@pytest.mark.parametrize("existing", [False, True])
def test_host_import_restores_output_when_lifecycle_state_replace_fails(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["sources"].unlink()
    original = paths["sources"].read_bytes() if existing else None
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    lifecycle_state = lifecycle.project_state_path(
        sync_cli.LIFECYCLE_ROOT,
        "project-1",
    )
    real_replace = lifecycle.os.replace
    failed = False

    def fail_lifecycle_replace(source, destination):  # type: ignore[no-untyped-def]
        nonlocal failed
        if Path(destination) == lifecycle_state and not failed:
            failed = True
            raise OSError("private-state-replace-detail")
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle.os, "replace", fail_lifecycle_replace)

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1

    public = capsys.readouterr().out
    assert "private-state-replace-detail" not in public
    assert (
        paths["sources"].read_bytes() if paths["sources"].exists() else None
    ) == original
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_host_import_restores_state_and_output_when_state_write_interrupts_after_commit(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["sources"].unlink()
    original_output = paths["sources"].read_bytes() if existing else None
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    original_write = lifecycle._write_state
    interrupted = False

    def commit_then_interrupt(path, payload):  # type: ignore[no-untyped-def]
        nonlocal interrupted
        original_write(path, payload)
        if payload["stage"] == "collected" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "_write_state", commit_then_interrupt)

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }
    assert (
        paths["sources"].read_bytes() if paths["sources"].exists() else None
    ) == original_output
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_host_import_restores_output_when_apply_is_interrupted_after_replace(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["sources"].unlink()
    original = paths["sources"].read_bytes() if existing else None
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    real_apply = sync_cli._RecoverableAtomicWrite.apply

    def interrupt_after_replace(operation) -> None:  # type: ignore[no-untyped-def]
        real_apply(operation)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "apply",
        interrupt_after_replace,
    )

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }
    assert (
        paths["sources"].read_bytes() if paths["sources"].exists() else None
    ) == original
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("existing", [False, True])
def test_host_import_apply_failure_rolls_back_without_state_write(
    tmp_path: Path,
    capsys,
    monkeypatch,
    existing: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    if not existing:
        paths["sources"].unlink()
    original = paths["sources"].read_bytes() if existing else None
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    real_apply = sync_cli._RecoverableAtomicWrite.apply
    state_write_count = 0

    def interrupt_after_replace(operation) -> None:  # type: ignore[no-untyped-def]
        real_apply(operation)
        raise KeyboardInterrupt

    def forbidden_state_write(*_args, **_kwargs) -> None:
        nonlocal state_write_count
        state_write_count += 1
        raise RuntimeError("private-state-writer-detail")

    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "apply",
        interrupt_after_replace,
    )
    monkeypatch.setattr(lifecycle, "_write_state", forbidden_state_write)

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }
    assert state_write_count == 0
    assert (
        paths["sources"].read_bytes() if paths["sources"].exists() else None
    ) == original
    lifecycle.assert_stage(
        "project-1",
        token,
        expected="begun",
        root=sync_cli.LIFECYCLE_ROOT,
        now=lambda: NOW,
    )


def test_host_import_rejects_existing_output_symlink_without_touching_target(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    target = tmp_path / "target.json"
    target.write_bytes(paths["sources"].read_bytes())
    paths["sources"].unlink()
    try:
        paths["sources"].symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    original = target.read_bytes()

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1

    assert target.read_bytes() == original
    assert paths["sources"].is_symlink()


def test_host_import_rejects_duplicate_import_with_same_run(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    args = host_import_args(paths, token)
    assert main(
        args,
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 0
    capsys.readouterr()
    written = paths["sources"].read_bytes()

    assert main(
        args,
        now=lambda: NOW,
        input_stream=io.BytesIO(host_import_payload()),
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "run_stage_invalid",
    }
    assert paths["sources"].read_bytes() == written


def test_host_import_normalizes_adapter_failure_without_leaking(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["sources"].read_bytes()
    assert main(["begin", "--project", "project-1"], now=lambda: NOW) == 0
    token = json.loads(capsys.readouterr().out)["run_token"]
    private_detail = "private-adapter-detail"

    assert main(
        host_import_args(paths, token),
        now=lambda: NOW,
        input_stream=io.BytesIO(
            host_import_payload(info={"nodeId": private_detail})
        ),
    ) == 1

    public = capsys.readouterr().out
    assert json.loads(public) == {
        "status": "error",
        "error_type": "host_import_invalid",
    }
    assert private_detail not in public
    assert token not in public
    assert paths["sources"].read_bytes() == original


def test_build_envelope_maps_statuses_and_uses_authoritative_hash() -> None:
    failed = DwsSourceRecord(
        source_type="task",
        source_id="task-private",
        permission_scope=SCOPE,
        fetched_at=NOW,
        status="failed",
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
        retry_after_seconds=3,
    )
    deleted = DwsSourceRecord(
        source_type="calendar",
        source_id="event-private",
        permission_scope=SCOPE,
        fetched_at=NOW,
        status="deleted",
    )
    selected = project(
        sources=(
            source_spec("document", "doc-1"),
            source_spec("task", "task-private"),
            source_spec("calendar", "event-private"),
        )
    )

    envelope = build_envelope(
        selected,
        bundle(active_record(), failed, deleted),
        context(),
        now=NOW,
    )

    from companion_gateway.project.sync_service import (
        compute_envelope_content_hash,
    )

    assert envelope.content_hash == compute_envelope_content_hash(envelope)
    assert envelope.generated_at == NOW
    assert envelope.context.generated_at == context().generated_at
    assert [item.status.value for item in envelope.sources] == ["active", "failed"]
    assert len(envelope.sources[0].chunks) == 1
    source_id_hash = digest("task-private")
    assert envelope.sources[1].source_title == f"task:{source_id_hash[:12]}"
    assert envelope.sources[1].source_url == f"dingtalk://task/{source_id_hash}"
    assert [item.status.value for item in envelope.tombstones] == ["deleted"]


def test_build_envelope_uses_push_clock_without_changing_semantic_identity() -> None:
    push_now = NOW + timedelta(seconds=301)
    source_bundle = bundle()
    project_context = context()

    first = build_envelope(
        project(), source_bundle, project_context, now=push_now
    )
    later = build_envelope(
        project(), source_bundle, project_context, now=push_now + timedelta(seconds=30)
    )

    assert first.generated_at == push_now
    assert first.context.generated_at == NOW
    assert first.sources[0].fetched_at == NOW
    assert later.content_hash == first.content_hash
    assert later.sync_id == first.sync_id


@pytest.mark.parametrize(
    "mutate,error_type",
    [
        (lambda data: data.update(project_id="other"), "context_mismatch"),
        (
            lambda data: data["source_refs"][0].update(source_title="伪造标题"),
            "source_ref_mismatch",
        ),
        (
            lambda data: data["source_refs"][0].update(excerpt="来源没有的事实"),
            "source_excerpt_mismatch",
        ),
    ],
)
def test_build_envelope_rejects_unanchored_qwen_facts(
    mutate, error_type: str
) -> None:  # type: ignore[no-untyped-def]
    data = context().model_dump(mode="json")
    mutate(data)
    with pytest.raises(ValueError, match=error_type):
        build_envelope(
            project(),
            bundle(),
            ProjectContextPackage.model_validate(data),
            now=NOW,
        )


@pytest.mark.parametrize(
    "field",
    ["sourced_actions", "sourced_risks", "sourced_next_meeting"],
)
def test_build_envelope_validates_each_sourced_fact_field(field: str) -> None:
    fact = sourced_fact()
    value: object = fact if field == "sourced_next_meeting" else (fact,)
    sourced_context = context().model_copy(update={field: value})

    envelope = build_envelope(project(), bundle(), sourced_context, now=NOW)

    assert getattr(envelope.context, field) == value


@pytest.mark.parametrize(
    "field",
    ["sourced_actions", "sourced_risks", "sourced_next_meeting"],
)
@pytest.mark.parametrize(
    ("records", "fact", "error_type"),
    [
        (
            (
                active_record(),
                DwsSourceRecord(
                    source_type="task",
                    source_id="task-1",
                    permission_scope=SCOPE,
                    fetched_at=NOW,
                    status="failed",
                    error_type=SourceErrorType.NETWORK_TIMEOUT,
                    retryable=True,
                ),
            ),
            sourced_fact(source_type="task", source_id="task-1"),
            "source_ref_mismatch",
        ),
        (
            (active_record(),),
            sourced_fact(excerpt="来源中不存在的摘录"),
            "source_excerpt_mismatch",
        ),
    ],
)
def test_build_envelope_rejects_invalid_sourced_fact_references(
    field: str,
    records: tuple[DwsSourceRecord, ...],
    fact: SourcedFact,
    error_type: str,
) -> None:
    selected = project(
        sources=tuple(
            source_spec(record.source_type.value, record.source_id)
            for record in records
        )
    )
    value: object = fact if field == "sourced_next_meeting" else (fact,)
    sourced_context = context().model_copy(update={field: value})

    with pytest.raises(ValueError, match=error_type):
        build_envelope(selected, bundle(*records), sourced_context, now=NOW)


def test_qwen_artifact_and_push_reject_nonempty_legacy_facts(
    tmp_path: Path,
    capsys,
) -> None:
    legacy_context = context().model_copy(
        update={"open_actions": ("无来源行动项",)}
    )
    with pytest.raises(ValidationError, match="context_fact_unreferenced"):
        QwenProjectContextArtifact(
            schema_version=1,
            context=legacy_context,
        )

    paths = write_push_inputs(tmp_path)
    write_json(
        paths["context"],
        {
            "schema_version": 1,
            "context": legacy_context.model_dump(mode="json"),
            "completed_retrieval_request_ids": [],
        },
    )

    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("invalid facts must fail first"),
        environ={},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "context_fact_unreferenced",
    }
    assert not paths["state"].exists()


def test_push_dry_run_never_uses_network_or_state(tmp_path: Path, capsys) -> None:
    paths = write_push_inputs(tmp_path)

    result = main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("dry-run must not use network"),
        environ={},
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["status"] == "ready"
    assert output["source_count"] == 1
    assert set(output) == {
        "status",
        "project_id",
        "source_count",
        "payload_bytes",
        "content_hash",
    }
    assert not paths["state"].exists()


@pytest.mark.parametrize(
    "gateway",
    [
        "https://127.0.0.1:8731",
        "http://127.0.0.1:8723",
        "http://localhost:8731?private=1",
        "http://localhost:8731/#private",
        "http://user@localhost:8731",
        "http://example.com:8731",
    ],
)
def test_push_rejects_nonlocal_or_ambiguous_gateway(
    tmp_path: Path, capsys, gateway: str
) -> None:
    paths = write_push_inputs(tmp_path)
    args = push_args(paths, "--dry-run")
    args[args.index("--gateway") + 1] = gateway

    assert main(args, environ={}) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "gateway_invalid",
    }


def test_push_uses_named_bearer_and_promotes_pending_state(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = RecordingUrlOpen()

    assert main(
        push_args(paths),
        urlopen=sent,
        environ={
            "COMPANION_DWS_SYNC_TOKEN": "private-token",
            "ARBITRARY_TOKEN": "must-not-be-used",
        },
    ) == 0

    output = json.loads(capsys.readouterr().out)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    request_payload = json.loads(sent.request.data)
    assert sent.request.full_url == (
        "http://127.0.0.1:8731/v1/projects/project-1/sync"
    )
    assert sent.request.get_header("Authorization") == "Bearer private-token"
    assert sent.request.get_header("Content-type") == "application/json"
    assert sent.timeout == 30.0
    assert state == {
        "last_artifact_hash": digest(paths["context"].read_text(encoding="utf-8")),
        "last_content_hash": request_payload["content_hash"],
        "last_cursor": 1,
        "last_source_semantic_hash": source_bundle_semantic_hash(
            DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
        ),
        "last_sync_id": request_payload["sync_id"],
        "pending": None,
        "project_id": "project-1",
        "schema_version": 1,
    }
    assert output["status"] == "synced"
    assert output["outcome"] == "applied"
    assert "sync_id" not in output
    assert "generation_id" not in output
    assert "private-token" not in canonical(output)
    approved_path = paths["context"].with_name("context.approved.json")
    assert approved_path.read_bytes() == paths["context"].read_bytes()

    lock_path = state_lock._state_lock_path(paths["state"], "project-1")
    assert lock_path.exists()
    assert lock_path.parent == state_lock.PRIVATE_LOCK_ROOT
    assert "project-1" not in lock_path.name


def test_concurrent_pushes_serialize_state_lifecycle_across_processes(
    tmp_path: Path,
) -> None:
    paths = write_push_inputs(tmp_path)
    process_context = multiprocessing.get_context("spawn")
    start_barrier = process_context.Barrier(3)
    load_barrier = process_context.Barrier(2)
    observations = process_context.Queue()
    processes = [
        process_context.Process(
            target=_concurrent_push_worker,
            args=(push_args(paths), start_barrier, load_barrier, observations),
        )
        for _ in range(2)
    ]

    try:
        for process in processes:
            process.start()
        start_barrier.wait(timeout=5)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    observed = [observations.get(timeout=2) for _ in range(6)]
    loaded = sorted(item[1:] for item in observed if item[0] == "loaded")
    sent = sorted(item[1:] for item in observed if item[0] == "sent")
    results = [item[1] for item in observed if item[0] == "result"]
    state = json.loads(paths["state"].read_text(encoding="utf-8"))

    assert loaded == [(0, True), (1, True)]
    assert [item[0] for item in sent] == [1, 2]
    assert len({item[1] for item in sent}) == 2
    assert results == [0, 0]
    assert state["last_cursor"] == 2
    assert state["pending"] is None


def test_push_lock_is_held_through_http_and_timeout_is_public(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    process_context = multiprocessing.get_context("spawn")
    entered_http = process_context.Event()
    release_http = process_context.Event()
    observations = process_context.Queue()
    holder = process_context.Process(
        target=_blocking_push_worker,
        args=(push_args(paths), entered_http, release_http, observations),
    )
    contender_sent = False

    def contender_urlopen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal contender_sent
        contender_sent = True
        return RecordingUrlOpen()(*_args, **_kwargs)

    try:
        holder.start()
        assert entered_http.wait(timeout=5)
        monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)

        assert main(
            push_args(paths),
            urlopen=contender_urlopen,
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error",
            "error_type": "sync_lock_timeout",
        }
        assert contender_sent is False
    finally:
        release_http.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    holder_observations = [observations.get(timeout=2) for _ in range(2)]
    assert [item[0] for item in holder_observations] == ["sent", "result"]


def test_state_lock_wait_is_capped_at_thirty_seconds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = iter((0.0, 30.0))
    sleeps: list[float] = []

    def unavailable(_stream: object) -> None:
        raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(state_lock, "_try_lock", unavailable)
    with pytest.raises(ValueError, match="^sync_lock_timeout$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            timeout=300.0,
            monotonic=lambda: next(clock),
            sleep=sleeps.append,
        ):
            pytest.fail("unavailable lock must not be yielded")

    assert sleeps == []


def test_state_lock_does_not_retry_non_contention_os_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def io_failure(_stream: object) -> None:
        raise OSError(errno.EIO, "private detail")

    monkeypatch.setattr(state_lock, "_try_lock", io_failure)
    with pytest.raises(ValueError, match="^private_file_write_failed$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            sleep=lambda _delay: pytest.fail("I/O errors must not be retried"),
        ):
            pytest.fail("failed lock must not be yielded")


def test_state_lock_does_not_retry_after_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempts = 0
    clock = iter((0.0, 29.99, 30.01))

    def available_too_late(_stream: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(state_lock, "_try_lock", available_too_late)
    with pytest.raises(ValueError, match="^sync_lock_timeout$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            monotonic=lambda: next(clock),
            sleep=lambda _delay: None,
        ):
            pytest.fail("lock acquired after deadline must not be yielded")

    assert attempts == 1


def test_state_lock_identity_depends_only_on_project_key(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"

    assert state_lock._state_lock_path(
        state_path, "project-1"
    ) == state_lock._state_lock_path(tmp_path / "other-state.json", "project-1")
    assert state_lock._state_lock_path(
        state_path, "project-1"
    ) != state_lock._state_lock_path(state_path, "project-2")


def test_same_project_different_state_files_share_one_lock(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    independent_paths = {**paths, "state": tmp_path / "independent-state.json"}
    process_context = multiprocessing.get_context("spawn")
    entered_http = process_context.Event()
    release_http = process_context.Event()
    observations = process_context.Queue()
    holder = process_context.Process(
        target=_blocking_push_worker,
        args=(push_args(paths), entered_http, release_http, observations),
    )

    try:
        holder.start()
        assert entered_http.wait(timeout=5)
        monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)

        assert main(
            push_args(independent_paths),
            urlopen=RecordingUrlOpen(),
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 1
        assert json.loads(capsys.readouterr().out)["error_type"] == (
            "sync_lock_timeout"
        )
    finally:
        release_http.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    assert not independent_paths["state"].exists()


def test_keyboard_interrupt_releases_push_lock(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)

    def interrupt(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    assert main(
        push_args(paths),
        urlopen=interrupt,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }

    monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)
    assert main(
        push_args(paths),
        urlopen=RecordingUrlOpen(),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "synced"


def test_push_requires_only_fixed_token_environment(tmp_path: Path, capsys) -> None:
    paths = write_push_inputs(tmp_path)

    assert main(
        push_args(paths),
        urlopen=lambda *_a, **_k: pytest.fail("missing token must fail first"),
        environ={"ARBITRARY_TOKEN": "wrong"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "token_missing",
    }
    assert not paths["state"].exists()


def test_failed_send_retains_pending_and_retry_reuses_identity(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)

    def unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise URLError("private-network-detail")

    assert main(
        push_args(paths),
        urlopen=unavailable,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    first_output = json.loads(capsys.readouterr().out)
    failed_state = json.loads(paths["state"].read_text(encoding="utf-8"))
    pending = failed_state["pending"]
    assert first_output == {"status": "error", "error_type": "network_error"}
    assert failed_state["last_source_semantic_hash"] is None
    assert failed_state["last_artifact_hash"] is None
    assert not paths["context"].with_name("context.approved.json").exists()

    sent = RecordingUrlOpen()
    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    assert json.loads(sent.request.data)["sync_id"] == pending["sync_id"]


def test_push_rolls_back_approved_when_state_promotion_interrupts(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    approved_path = paths["context"].with_name("context.approved.json")
    selected = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    old_approved = QwenProjectContextArtifact(
        schema_version=1,
        context=context(excerpt="采用 方案"),
    )
    write_json(approved_path, old_approved.model_dump(mode="json"))
    write_json(paths["state"], semantic_state(selected, old_approved))
    old_approved_bytes = approved_path.read_bytes()
    real_apply = sync_cli._RecoverableAtomicWrite.apply
    pending_state_bytes: list[bytes] = []

    def fail_state_after_replace(operation) -> None:  # type: ignore[no-untyped-def]
        if operation._path == paths["state"]:
            pending_state_bytes.append(paths["state"].read_bytes())
        real_apply(operation)
        if operation._path == paths["state"]:
            raise RuntimeError("private-state-detail")

    monkeypatch.setattr(
        sync_cli._RecoverableAtomicWrite,
        "apply",
        fail_state_after_replace,
    )

    first_send = RecordingUrlOpen()
    assert main(
        push_args(paths),
        urlopen=first_send,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "error", "error_type": "sync_failed"}
    assert "private-state-detail" not in output
    assert approved_path.read_bytes() == old_approved_bytes
    assert pending_state_bytes
    assert paths["state"].read_bytes() == pending_state_bytes[0]
    failed_state = SyncCliState.model_validate_json(paths["state"].read_bytes())
    assert failed_state.pending is not None
    assert failed_state.last_artifact_hash == artifact_hash(old_approved)
    assert failed_state.last_source_semantic_hash == source_bundle_semantic_hash(
        selected
    )

    monkeypatch.setattr(sync_cli._RecoverableAtomicWrite, "apply", real_apply)
    retry_send = RecordingUrlOpen()
    assert main(
        push_args(paths),
        urlopen=retry_send,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    first_envelope = json.loads(first_send.request.data)
    retry_envelope = json.loads(retry_send.request.data)
    for field in (
        "source_cursor",
        "sync_id",
        "content_hash",
        "completed_retrieval_claims",
    ):
        assert retry_envelope[field] == first_envelope[field]


def test_changed_content_conflicts_with_pending_without_network(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    write_json(
        paths["state"],
        {
            "schema_version": 1,
            "project_id": "project-1",
            "last_cursor": 0,
            "last_content_hash": None,
            "last_sync_id": None,
            "pending": {
                "source_cursor": 1,
                "content_hash": "f" * 64,
                "sync_id": "sync_" + "e" * 32,
            },
        },
    )

    assert main(
        push_args(paths),
        urlopen=lambda *_a, **_k: pytest.fail("conflict must not use network"),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "pending_sync_conflict",
    }


@pytest.mark.parametrize(
    "response,error_type",
    [
        ({"result": {}}, "response_invalid"),
        (
            {
                "sync_id": "wrong-sync",
                "outcome": "applied",
                "project_status": "healthy",
                "accepted_sources": 1,
                "failed_sources": 0,
                "generation_id": None,
                "next_sync_before": "2026-09-05T12:05:00+00:00",
            },
            "response_sync_mismatch",
        ),
    ],
)
def test_invalid_success_response_is_sanitized_and_keeps_pending(
    tmp_path: Path,
    capsys,
    response: dict[str, object],
    error_type: str,
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = RecordingUrlOpen(response)

    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": error_type,
    }
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert state["pending"]
    assert state["last_source_semantic_hash"] is None
    assert state["last_artifact_hash"] is None
    assert not paths["context"].with_name("context.approved.json").exists()


def test_payload_over_limit_fails_before_state_or_network(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    huge = "# 大文档\n采用方案 B。\n" + "x" * 2_080_000
    write_json(
        paths["sources"],
        bundle(active_record(content=huge)).model_dump(mode="json"),
    )

    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("oversize must not use network"),
        environ={},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "payload_too_large",
    }
    assert not paths["state"].exists()


@pytest.mark.parametrize(
    ("file_key", "limit", "error_type", "dry_run"),
    [
        ("sources", 2_097_152, "sources_file_too_large", True),
        ("context", 2_097_152, "context_file_too_large", True),
        ("state", 65_536, "state_file_too_large", False),
    ],
)
def test_private_input_limits_fail_before_network_or_state_write(
    tmp_path: Path,
    capsys,
    file_key: str,
    limit: int,
    error_type: str,
    dry_run: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    oversized = b"x" * (limit + 1)
    paths[file_key].write_bytes(oversized)
    extra = ("--dry-run",) if dry_run else ()

    assert main(
        push_args(paths, *extra),
        urlopen=lambda *_a, **_k: pytest.fail("size gate must precede network"),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": error_type,
    }
    if file_key == "state":
        assert paths["state"].read_bytes() == oversized
    else:
        assert not paths["state"].exists()


def test_internal_error_text_is_never_exposed(tmp_path: Path, capsys) -> None:
    paths = write_push_inputs(tmp_path)

    def private_failure() -> datetime:
        raise ValueError("private_secret_detail")

    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("failure must precede network"),
        environ={},
        now=private_failure,
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "sync_failed",
    }


def test_default_transport_disables_environment_and_system_proxies(
    monkeypatch,
) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(
        "urllib.request.getproxies",
        lambda: pytest.fail("system proxies must not be loaded"),
    )
    captured_handlers: list[object] = []
    real_build_opener = sync_cli.build_opener

    def recording_build_opener(*handlers: object):
        captured_handlers.extend(handlers)
        return real_build_opener(*handlers)

    monkeypatch.setattr(sync_cli, "build_opener", recording_build_opener)

    sync_cli._build_direct_opener()

    proxy_handlers = [
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_default_transport_rejects_redirect_without_forwarding_bearer(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    target_authorizations: list[str | None] = []
    redirect_requests = 0

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_authorizations.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal redirect_requests
            redirect_requests += 1
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/outside",
            )
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setattr(
            sync_cli,
            "_gateway_base",
            lambda _value: f"http://127.0.0.1:{redirect.server_port}",
        )
        assert main(
            push_args(paths),
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 1
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert redirect_requests == 1
    assert target_authorizations == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "http_error",
    }
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert state["pending"]
    assert state["last_source_semantic_hash"] is None
    assert state["last_artifact_hash"] is None
    assert not paths["context"].with_name("context.approved.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [("outcome", []), ("project_status", {"private": "value"})],
)
def test_response_enum_fields_require_strings(
    tmp_path: Path,
    capsys,
    field: str,
    value: object,
) -> None:
    paths = write_push_inputs(tmp_path)
    response = {
        "sync_id": "placeholder",
        "outcome": "applied",
        "project_status": "healthy",
        "accepted_sources": 1,
        "failed_sources": 0,
        "generation_id": None,
        "next_sync_before": "2026-09-05T12:05:00+00:00",
    }

    class InvalidEnumResponse(RecordingUrlOpen):
        def __call__(self, request, *, timeout):  # type: ignore[no-untyped-def]
            request_payload = json.loads(request.data)
            response["sync_id"] = request_payload["sync_id"]
            response[field] = value
            return super().__call__(request, timeout=timeout)

    sent = InvalidEnumResponse(response)
    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "response_invalid",
    }
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


def test_response_read_is_bounded_to_sixty_four_kibibytes(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = RecordingUrlOpen()

    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    assert sent.read_sizes == [65_537]


def test_oversized_response_is_rejected_after_one_bounded_read(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    read_sizes: list[int] = []

    def oversized(_request, *, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 30.0

        def read(size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

        return SimpleNamespace(read=read)

    assert main(
        push_args(paths),
        urlopen=oversized,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "response_invalid",
    }
    assert read_sizes == [65_537]
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


@pytest.mark.parametrize("argv", [["--help"], ["collect", "--help"]])
def test_help_outputs_exactly_one_json_object(
    capsys, argv: list[str]
) -> None:
    assert main(argv) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == "help"
    assert output.err == ""
    assert output.out.count("\n") == 1


def test_qwen_prompt_only_completes_retrieval_with_obtained_evidence() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    normalized = " ".join(prompt.replace("`", "").split())
    assert "未完成的检索请求绝不能加入 completed_retrieval_request_ids" in normalized
    assert "只有已取得对应证据" in normalized
    assert "遗漏的请求保持 pending" in normalized
    assert "python tools/dws_sync_runtime.py pending" in normalized
    assert "retrieval_requests" in normalized
    assert "request_id" in normalized
    assert "query_hash" in normalized
    assert "sources" in normalized
    assert "request_epoch" in normalized
    assert "attempt_count" in normalized
    assert "lease_token" in normalized
    collect_at = normalized.index("python tools/dws_sync_runtime.py host-import")
    pending_at = normalized.index("python tools/dws_sync_runtime.py pending")
    skill_at = normalized.index("hui-anchor-dws-project-context-v1", pending_at)
    push_at = normalized.index("python tools/dws_sync_runtime.py push")
    assert collect_at < pending_at < skill_at < push_at


def test_qwen_prompt_fences_full_lifecycle_and_always_releases() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())

    for command in ("begin", "host-import", "pending", "artifact", "push", "end"):
        assert f"python tools/dws_sync_runtime.py {command}" in normalized
    assert "python tools/dws_sync_runtime.py abort" in normalized
    assert "--run-token" in normalized
    assert "finally" in normalized
    assert "coalesced" in normalized
    assert "不得直接写 context_artifact" in normalized
    assert "completed_retrieval_claims" in normalized

    begin_at = normalized.index("python tools/dws_sync_runtime.py begin")
    collect_at = normalized.index("python tools/dws_sync_runtime.py host-import")
    artifact_at = normalized.index("python tools/dws_sync_runtime.py artifact")
    push_at = normalized.index("python tools/dws_sync_runtime.py push")
    end_at = normalized.index("python tools/dws_sync_runtime.py end")
    assert begin_at < collect_at < artifact_at < push_at < end_at


def test_qwen_prompt_uses_protected_runtime_entrypoints() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "python tools/dws_sync_runtime.py host-import" in prompt
    assert "python tools/dws_sync_runtime.py push" in prompt
    assert "credential.dpapi" in prompt
    assert "python tools/dws_sync_runtime.py collect" not in prompt
    assert "tools/dws_project_sync.py collect" not in prompt
    assert "tools/dws_project_sync.py push" not in prompt


def test_qwen_prompt_uses_three_independent_host_collection_calls() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())

    assert "doc info" in normalized
    assert "doc read" in normalized
    assert "--format json" in normalized
    assert 'tojson as $raw | {encoding:"base64-json",byte_count:($raw|utf8bytelength),payload:($raw|@base64)}' in prompt
    assert "三个独立工具调用" in normalized
    assert "引用 here-document" in normalized
    assert "不得使用管道" in normalized
    assert "不得使用命令替换" in normalized
    assert "不得使用 Popen" in normalized
    assert "不得向用户输出封包" in normalized
    assert "不得写临时文件" in normalized
    assert "host-import 成功后" in normalized
    assert "pending-post-tool-use" in normalized


def test_qwen_prompt_defines_fixed_strict_private_task_config() -> None:
    root = Path(__file__).resolve().parents[2]
    prompt = (root / "prompts" / "qwenwork-dws-project-sync.md").read_text(
        encoding="utf-8"
    )
    schema_match = re.search(
        r"<!-- task-config-schema -->\s*```json\s*(\{.*?\})\s*```",
        prompt,
        re.DOTALL,
    )

    assert schema_match is not None
    schema = json.loads(schema_match.group(1))
    fields = {
        "schema_version",
        "manifest",
        "project",
        "dws",
        "source_bundle",
        "context_artifact",
        "state",
    }
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == fields
    assert set(schema["properties"]) == fields
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["properties"]["project"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    assert schema["properties"]["dws"]["pattern"] == "^[CcEe]:\\\\"
    for field in ("manifest", "source_bundle", "context_artifact", "state"):
        assert schema["properties"][field]["pattern"] == "^[Ee]:\\\\"
    assert "官方 wrapper" in prompt
    assert "任意命令文本" in prompt
    assert ".private/qwenwork-dws-project-sync.json" in prompt
    forbidden_placeholder = "<" + "PRIVATE_"
    assert forbidden_placeholder not in prompt
    lstat_at = prompt.index("Path.lstat")
    read_at = prompt.index("read(65537)")
    parse_at = prompt.index("JSON 解析")
    assert "普通文件" in prompt[lstat_at:read_at]
    assert "symlink" in prompt[lstat_at:read_at]
    assert "reparse" in prompt[lstat_at:read_at]
    assert lstat_at < read_at < parse_at


def test_qwen_prompt_rejects_private_path_aliases() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "Path.resolve(strict=False)" in prompt
    assert "os.path.normcase" in prompt
    assert "os.path.samefile" in prompt
    assert "五个配置路径与固定任务配置路径必须两两不同" in prompt
    assert "reparse" in prompt


def test_qwen_prompt_names_skill_and_closes_artifact_io_contract() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "hui-anchor-dws-project-context-v1" in prompt
    assert "DwsSourceBundle" in prompt
    assert "QwenProjectContextArtifact" in prompt
    assert "唯一输入" in prompt
    assert "唯一输出" in prompt
    assert "同目录临时文件" in prompt
    assert "flush" in prompt
    assert "fsync" in prompt
    assert "os.replace" in prompt
    assert "不得读取其他文件" in prompt
    assert "不得输出其他内容" in prompt
    assert '"open_actions": []' in prompt
    assert '"current_risks": []' in prompt
    assert '"next_meeting": null' in prompt
    assert "必须预先安装" in prompt
    assert "不可用时立即停止" in prompt
    assert "不得搜索、安装或替换 Skill" in prompt

    normalized = " ".join(prompt.split())
    validate_at = normalized.index("QwenProjectContextArtifact.model_validate")
    size_at = normalized.index("2097152")
    temporary_at = normalized.index("创建同目录临时文件")
    replace_at = normalized.index("os.replace")
    assert validate_at < temporary_at
    assert size_at < temporary_at
    assert temporary_at < replace_at


def test_skill_contract_requires_single_body_excerpt_without_rewriting() -> None:
    root = Path(__file__).resolve().parents[2]
    documents = (
        (root / "skills/hui-anchor-dws-project-context-v1/SKILL.md").read_text(
            encoding="utf-8"
        ),
        (root / "skills/hui-anchor-dws-project-context-v1/contract.md").read_text(
            encoding="utf-8"
        ),
    )

    for document in documents:
        normalized = " ".join(document.split())
        assert "单个非标题正文片段" in normalized
        assert "连续原文" in normalized
        assert "不拼接" in normalized
        assert "不省略" in normalized
        assert "不改标点" in normalized
        assert "最长 150 字" in normalized
        assert "省略该事实" in normalized


def test_qwen_prompt_stops_after_failures_and_only_reruns_after_end() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())
    compact = "".join(prompt.replace("`", "").split())

    begin_once_at = compact.index("单个调度触发最多一次begin")
    failure_at = compact.index("任何命令非成功", begin_once_at)
    save_error_at = compact.index(
        "先在内存中保存该失败命令返回的固定错误",
        failure_at,
    )
    abort_at = compact.index("同一token调用abort", save_error_at)
    output_at = compact.index("原样输出固定错误", abort_at)
    return_at = compact.index("return", output_at)
    no_begin_at = compact.index("abort后不得begin", return_at)
    rerun_only_at = compact.index("只有end=rerun", no_begin_at)
    full_rerun_at = compact.index("完整重跑", rerun_only_at)
    recollect_at = compact.index("每轮重新采集", full_rerun_at)
    replay_ban_at = compact.index(
        "禁止读取或回放context_artifact",
        recollect_at,
    )
    retry_ban_at = compact.index(
        "确定性push错误不得再次push",
        replay_ban_at,
    )
    numbered_flow_at = compact.index(
        "1.使用参数数组运行pythontools/dws_sync_runtime.pybegin",
        retry_ban_at,
    )
    assert begin_once_at < failure_at < save_error_at < abort_at
    assert abort_at < output_at < return_at < no_begin_at
    assert no_begin_at < rerun_only_at < full_rerun_at < recollect_at
    assert recollect_at < replay_ban_at < retry_ban_at < numbered_flow_at

    end_step_at = normalized.index(
        "12. push 成功后，以同一 token 运行 python tools/dws_sync_runtime.py end"
    )
    rerun_response_at = normalized.index("返回 rerun 时", end_step_at)
    rerun_chain_at = normalized.index(
        "完整的宿主双 DWS 采集 -> host-import -> pending -> reuse-artifact ->（artifact_reused，或 artifact_required -> Skill -> artifact）-> push -> end 链路",
        rerun_response_at,
    )
    final_abort_at = normalized.index(
        "任何未成功 end 的路径都必须由 finally 调用 abort",
        rerun_chain_at,
    )
    assert end_step_at < rerun_response_at < rerun_chain_at < final_abort_at
    assert " begin" not in normalized[rerun_response_at:final_abort_at]


def test_qwen_prompt_reuses_approved_artifact_before_skill() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())

    pending_at = normalized.index("python tools/dws_sync_runtime.py pending")
    reuse_at = normalized.index(
        "python tools/dws_sync_runtime.py reuse-artifact",
        pending_at,
    )
    skill_at = normalized.index("hui-anchor-dws-project-context-v1", reuse_at)
    assert pending_at < reuse_at < skill_at
    assert "artifact_reused" in normalized[reuse_at:skill_at]
    assert "artifact_required" in normalized[reuse_at:skill_at]
    rerun_at = normalized.index("返回 rerun 时")
    rerun_reuse_at = normalized.index("reuse-artifact", rerun_at)
    rerun_skill_at = normalized.index("Skill", rerun_reuse_at)
    rerun_end_at = normalized.index("end 链路", rerun_skill_at)
    assert rerun_at < rerun_reuse_at < rerun_skill_at < rerun_end_at


def test_private_task_config_is_ignored_and_documented_publicly() -> None:
    root = Path(__file__).resolve().parents[2]
    ignore_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    readme = (root / "README.md").read_text(encoding="utf-8")
    gateway_readme = (root / "gateway" / "README.md").read_text(
        encoding="utf-8"
    )

    assert ".private/" in ignore_lines
    for document in (readme, gateway_readme):
        assert ".private/qwenwork-dws-project-sync.json" in document
        assert "hui-anchor-dws-project-context-v1" in document
