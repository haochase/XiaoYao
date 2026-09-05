from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import companion_gateway.api as device_api
from companion_gateway.api import create_app
from companion_gateway.project.auth import ProjectApiPrincipal
from companion_gateway.project.index import ProjectSnapshotRegistry
from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    RetrievalRequestStatus,
    SourceSnapshot,
    SourceSyncStatus,
    SourceTombstone,
    SyncEnvelope,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import ProjectSyncRepository
from companion_gateway.project.sync_service import (
    ProjectSyncService,
    compute_envelope_content_hash,
)
from companion_gateway.settings import Settings


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
PROJECT_ID = "project-1"
SCOPE = "project:demo"
TOKEN = "project-token"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReversibleProtector:
    protector_version = "test-reversible-v1"

    def protect(self, project_id: str, plaintext: bytes) -> bytes:
        return project_id.encode("utf-8") + b"\0" + plaintext

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        prefix = project_id.encode("utf-8") + b"\0"
        if not protected.startswith(prefix):
            raise RuntimeError("wrong_project")
        return protected[len(prefix) :]


def principal() -> ProjectApiPrincipal:
    return ProjectApiPrincipal(
        principal_id="qwenwork",
        token_sha256=digest(TOKEN),
        project_ids=frozenset({PROJECT_ID}),
        permission_scopes=frozenset({SCOPE}),
    )


def source_ref() -> EvidenceRef:
    return EvidenceRef(
        source_type="document",
        source_id="doc-1",
        source_title="Decision document",
        source_url="dingtalk://doc/doc-1",
        source_time=NOW,
        excerpt="Use plan B",
        permission_scope=SCOPE,
    )


def context(at: datetime) -> ProjectContextPackage:
    reference = source_ref()
    return ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="Demo project",
        generated_at=at,
        source_refs=(reference,),
        active_decisions=(
            DecisionCard(
                decision_id="decision-1",
                project_id=PROJECT_ID,
                topic="terminal plan",
                decision_text="Use plan B",
                rationale="Stable rollout",
                owner="project-owner",
                decided_at=NOW,
                source_refs=(reference,),
                status="active",
                confidence=0.9,
            ),
        ),
        permission_scope=SCOPE,
        freshness_seconds=1_800,
    )


def source_snapshot(
    at: datetime,
    *,
    text: str,
    version: str,
) -> SourceSnapshot:
    end = len(text)
    chunk_id = digest(
        json.dumps(
            {
                "end_offset": end,
                "heading_path": ("Decision",),
                "ordinal": 0,
                "source_id": "doc-1",
                "start_offset": 0,
                "text": text,
                "version": version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return SourceSnapshot(
        source_type=SyncSourceType.DOCUMENT,
        source_id="doc-1",
        source_title="Decision document",
        source_url="dingtalk://doc/doc-1",
        source_version=version,
        source_time=NOW,
        fetched_at=at,
        permission_scope=SCOPE,
        permission_hash=digest("permission"),
        status=SourceSyncStatus.ACTIVE,
        chunks=(
            EvidenceChunk(
                chunk_id=chunk_id,
                source_id="doc-1",
                source_version=version,
                ordinal=0,
                heading_path=("Decision",),
                text=text,
                start_offset=0,
                end_offset=end,
                content_hash=digest(text),
            ),
        ),
        content_hash=digest(text),
    )


def active_source(at: datetime) -> SourceSnapshot:
    return source_snapshot(
        at,
        text="Use plan B for the terminal rollout.",
        version="v1",
    )


def envelope(cursor: int, at: datetime, *, revoked: bool = False) -> SyncEnvelope:
    draft = SyncEnvelope(
        schema_version=1,
        sync_id=f"sync-{cursor}",
        project_id=PROJECT_ID,
        generated_at=at,
        source_cursor=cursor,
        content_hash="0" * 64,
        producer="qwenwork-dws",
        context=context(at),
        sources=() if revoked else (active_source(at),),
        tombstones=(
            SourceTombstone(
                source_type=SyncSourceType.DOCUMENT,
                source_id="doc-1",
                status=SourceSyncStatus.REVOKED,
                occurred_at=at,
                permission_scope=SCOPE,
            ),
        )
        if revoked
        else (),
    )
    return draft.model_copy(
        update={"content_hash": compute_envelope_content_hash(draft)}
    )


def settings(database_path: Path) -> Settings:
    return Settings(
        database_path=database_path,
        project_api_principals=(principal(),),
    )


def unstructured_envelope(
    cursor: int,
    at: datetime,
    *,
    text: str,
    version: str,
) -> SyncEnvelope:
    reference = source_ref().model_copy(update={"excerpt": "Shared anchor"})
    package = ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="Demo project",
        generated_at=at,
        source_refs=(reference,),
        permission_scope=SCOPE,
        freshness_seconds=1_800,
    )
    draft = SyncEnvelope(
        schema_version=1,
        sync_id=f"sync-unstructured-{cursor}",
        project_id=PROJECT_ID,
        generated_at=at,
        source_cursor=cursor,
        content_hash="0" * 64,
        producer="qwenwork-dws",
        context=package,
        sources=(
            source_snapshot(
                at,
                text=text,
                version=version,
            ),
        ),
    )
    return draft.model_copy(
        update={"content_hash": compute_envelope_content_hash(draft)}
    )


def configure_device_dependencies(monkeypatch, protector=ReversibleProtector) -> None:
    monkeypatch.setattr(
        device_api,
        "WindowsDpapiProtector",
        protector,
        raising=False,
    )
    monkeypatch.setattr(
        device_api,
        "protection_identity_digest",
        lambda: digest("test-user"),
        raising=False,
    )


def test_independent_device_app_refreshes_shared_sqlite_and_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "shared-project.db"
    repository = ProjectSyncRepository(database_path)
    repository.initialize()
    repository.configure_protection(
        digest("test-user"),
        ReversibleProtector.protector_version,
    )
    sync_service = ProjectSyncService(
        repository,
        ReversibleProtector(),
        ProjectSnapshotRegistry(),
    )
    sync_service.apply(envelope(1, NOW), principal=principal(), now=NOW)
    configure_device_dependencies(monkeypatch)

    device_app = create_app(
        settings(database_path),
        project_clock=lambda: NOW + timedelta(minutes=1),
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(device_app) as client:
        first = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "terminal plan", "kind": "decision_check"},
            headers=headers,
        )
        assert first.status_code == 200

        revoked_at = NOW + timedelta(minutes=2)
        sync_service.apply(
            envelope(2, revoked_at, revoked=True),
            principal=principal(),
            now=revoked_at,
        )
        blocked = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "terminal plan", "kind": "decision_check"},
            headers=headers,
        )

    assert blocked.status_code == 404
    assert blocked.json()["detail"] == "source_unavailable"


def test_device_query_refreshes_new_generation_evidence_from_shared_sqlite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "shared-update.db"
    repository = ProjectSyncRepository(database_path)
    repository.initialize()
    repository.configure_protection(
        digest("test-user"),
        ReversibleProtector.protector_version,
    )
    writer = ProjectSyncService(
        repository,
        ReversibleProtector(),
        ProjectSnapshotRegistry(),
    )
    writer.apply(
        unstructured_envelope(
            1,
            NOW,
            text="Shared anchor. Alpha evidence.",
            version="v1",
        ),
        principal=principal(),
        now=NOW,
    )
    configure_device_dependencies(monkeypatch)
    current_time = {"value": NOW + timedelta(minutes=1)}
    device_app = create_app(
        settings(database_path),
        project_clock=lambda: current_time["value"],
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(device_app) as client:
        first = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "Alpha evidence", "kind": "fact"},
            headers=headers,
        )
        assert first.status_code == 200
        assert "Alpha evidence" in first.json()["answer"]["text"]

        updated_at = NOW + timedelta(minutes=2)
        writer.apply(
            unstructured_envelope(
                2,
                updated_at,
                text="Shared anchor. Beta evidence.",
                version="v2",
            ),
            principal=principal(),
            now=updated_at,
        )
        current_time["value"] = updated_at
        updated = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "Beta evidence", "kind": "fact"},
            headers=headers,
        )

    assert updated.status_code == 200
    assert "Beta evidence" in updated.json()["answer"]["text"]


def test_device_retrieval_request_is_visible_to_sync_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "shared-retrieval.db"
    repository = ProjectSyncRepository(database_path)
    repository.initialize()
    repository.configure_protection(
        digest("test-user"),
        ReversibleProtector.protector_version,
    )
    writer = ProjectSyncService(
        repository,
        ReversibleProtector(),
        ProjectSnapshotRegistry(),
    )
    writer.apply(
        unstructured_envelope(
            1,
            NOW,
            text="Shared anchor. Existing evidence.",
            version="v1",
        ),
        principal=principal(),
        now=NOW,
    )
    configure_device_dependencies(monkeypatch)
    device_app = create_app(
        settings(database_path),
        project_clock=lambda: NOW + timedelta(minutes=1),
    )

    with TestClient(device_app) as client:
        response = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "完全无关的深度问题", "kind": "fact"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    requests = repository.list_retrieval_requests(
        PROJECT_ID,
        RetrievalRequestStatus.PENDING,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "evidence_pending"
    assert len(requests) == 1
    assert requests[0].source_id_hashes == (digest("doc-1"),)


def test_device_query_never_falls_back_when_active_ciphertext_cannot_decrypt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class BrokenProtector(ReversibleProtector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            raise RuntimeError("private-decryption-detail")

    database_path = tmp_path / "shared-corrupt.db"
    repository = ProjectSyncRepository(database_path)
    repository.initialize()
    repository.configure_protection(
        digest("test-user"),
        ReversibleProtector.protector_version,
    )
    writer = ProjectSyncService(
        repository,
        ReversibleProtector(),
        ProjectSnapshotRegistry(),
    )
    writer.apply(envelope(1, NOW), principal=principal(), now=NOW)
    configure_device_dependencies(monkeypatch, BrokenProtector)
    device_app = create_app(
        settings(database_path),
        project_clock=lambda: NOW + timedelta(minutes=1),
    )

    with TestClient(device_app) as client:
        response = client.post(
            f"/v1/projects/{PROJECT_ID}/query",
            json={"query": "terminal plan", "kind": "decision_check"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "source_unavailable"
    assert "private-decryption-detail" not in response.text
