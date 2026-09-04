from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import companion_gateway.sync_api as sync_api_module
from companion_gateway.api import create_app
from companion_gateway.project import (
    EvidenceChunk,
    EvidenceRef,
    ProjectContextPackage,
    ProjectSyncHealth,
    ProjectSyncStatus,
    SourceSnapshot,
    SourceState,
    SourceSyncStatus,
    SyncEnvelope,
    SyncResult,
    SyncSourceType,
)
from companion_gateway.project.auth import ProjectApiPrincipal
from companion_gateway.project.sync_service import (
    ProjectSourceUnavailable,
    ProjectSyncValidationError,
    compute_envelope_content_hash,
)
from companion_gateway.settings import Settings
from companion_gateway.sync_api import create_sync_app


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
TOKEN = "qwenwork-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PROJECT_ID = "project-1"
PERMISSION_SCOPE = "project:demo"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def source_ref(source_id: str = "doc-1") -> EvidenceRef:
    return EvidenceRef(
        source_type="document",
        source_id=source_id,
        source_title="Decision document",
        source_url=f"dingtalk://doc/{source_id}",
        source_time=NOW - timedelta(minutes=10),
        excerpt="Use plan B",
        permission_scope=PERMISSION_SCOPE,
    )


def envelope() -> SyncEnvelope:
    text = "Use plan B for the terminal rollout."
    chunk = EvidenceChunk(
        chunk_id=digest(
            json.dumps(
                {
                    "end_offset": len(text),
                    "heading_path": ("Decision",),
                    "ordinal": 0,
                    "source_id": "doc-1",
                    "start_offset": 0,
                    "text": text,
                    "version": "v1",
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
        source_id="doc-1",
        source_version="v1",
        ordinal=0,
        heading_path=("Decision",),
        text=text,
        start_offset=0,
        end_offset=len(text),
        content_hash=digest(text),
    )
    package = ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="Demo project",
        generated_at=NOW,
        source_refs=(source_ref(),),
        permission_scope=PERMISSION_SCOPE,
        freshness_seconds=300,
    )
    draft = SyncEnvelope(
        schema_version=1,
        sync_id="sync-1",
        project_id=PROJECT_ID,
        generated_at=NOW,
        source_cursor=1,
        content_hash="0" * 64,
        producer="qwenwork-dws",
        context=package,
        sources=(
            SourceSnapshot(
                source_type=SyncSourceType.DOCUMENT,
                source_id="doc-1",
                source_title="Decision document",
                source_url="dingtalk://doc/doc-1",
                source_version="v1",
                source_time=NOW - timedelta(minutes=10),
                fetched_at=NOW,
                permission_scope=PERMISSION_SCOPE,
                permission_hash=digest("permission"),
                status=SourceSyncStatus.ACTIVE,
                chunks=(chunk,),
                content_hash=digest("content"),
            ),
        ),
    )
    return draft.model_copy(
        update={"content_hash": compute_envelope_content_hash(draft)}
    )


def envelope_json() -> dict[str, object]:
    return envelope().model_dump(mode="json")


def settings(database_path) -> Settings:
    return Settings(
        database_path=database_path,
        project_api_principals=(
            ProjectApiPrincipal(
                principal_id="qwenwork",
                token_sha256=digest(TOKEN),
                project_ids=frozenset({PROJECT_ID}),
                permission_scopes=frozenset({PERMISSION_SCOPE}),
            ),
        ),
    )


class StubSyncService:
    def __init__(self) -> None:
        self.applied: list[SyncEnvelope] = []
        self.required_refs: list[tuple[EvidenceRef, ...]] = []
        self.apply_error: Exception | None = None
        self.source_error: Exception | None = None

    def apply(self, package, *, principal, now):  # type: ignore[no-untyped-def]
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append(package)
        return SyncResult(
            sync_id=package.sync_id,
            outcome="applied",
            project_status=ProjectSyncHealth.HEALTHY,
            accepted_sources=1,
            failed_sources=0,
            generation_id="generation-1",
            next_sync_before=now + timedelta(minutes=5),
        )

    def status(self, project_id, *, now):  # type: ignore[no-untyped-def]
        if project_id != PROJECT_ID:
            raise ProjectSourceUnavailable("project_not_synced")
        return ProjectSyncStatus(
            project_id=project_id,
            health=ProjectSyncHealth.HEALTHY,
            sources=(
                SourceState(
                    project_id=project_id,
                    source_type=SyncSourceType.DOCUMENT,
                    source_id_hash=digest("doc-1"),
                    source_version="v1",
                    content_hash=digest("content"),
                    permission_hash=digest("permission"),
                    status=SourceSyncStatus.ACTIVE,
                    last_attempt_at=now,
                    last_success_at=now,
                    last_error_type=None,
                ),
            ),
            last_success_at=now,
            next_sync_before=now + timedelta(minutes=5),
        )

    def require_sources_fresh(
        self,
        project_id,
        source_refs,
        *,
        now,
    ):  # type: ignore[no-untyped-def]
        if self.source_error is not None:
            raise self.source_error
        assert project_id == PROJECT_ID
        self.required_refs.append(source_refs)


@pytest.fixture
def sync_service() -> StubSyncService:
    return StubSyncService()


@pytest.fixture
def client(tmp_path, sync_service: StubSyncService) -> Iterator[TestClient]:
    app = create_sync_app(
        settings(tmp_path / "sync-api.db"),
        sync_service=sync_service,
        clock=lambda: NOW,
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1:8731",
    ) as test_client:
        yield test_client


def test_sync_api_health_and_ready_are_loopback_only(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["status"] == "ready"
    rejected = client.get(
        "/health",
        headers={"Host": "127.0.0.1:8723"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "sync_host_forbidden"


@pytest.mark.parametrize(
    "header",
    ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Original-URL"],
)
def test_sync_api_rejects_forwarded_headers(
    client: TestClient,
    header: str,
) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        json=envelope_json(),
        headers={**AUTH, header: "127.0.0.1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "sync_proxy_headers_forbidden"


def test_sync_api_rejects_oversized_body_before_json_parsing(
    client: TestClient,
) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        content=b"{" + b"x" * 2_097_152,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "sync_body_too_large"


def test_sync_api_rejects_oversized_stream_without_content_length(
    client: TestClient,
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"{"
        yield b"x" * 2_097_152

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        content=chunks(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "sync_body_too_large"


def test_sync_middleware_rejects_one_oversized_chunk_before_extending(
    monkeypatch,
) -> None:
    class ExtendForbiddenBytearray(bytearray):
        def extend(self, value) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("oversized chunk was extended")

    async def exercise() -> list[dict[str, object]]:
        async def downstream(_scope, _receive, _send) -> None:
            raise AssertionError("oversized request reached downstream")

        middleware = sync_api_module._SyncBoundaryMiddleware(
            downstream,
            max_body_bytes=1_024,
        )
        messages = [
            {
                "type": "http.request",
                "body": b"x" * 1_025,
                "more_body": False,
            }
        ]
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return messages.pop(0)

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "headers": ((b"host", b"127.0.0.1:8731"),),
            },
            receive,
            send,
        )
        return sent

    monkeypatch.setattr(
        sync_api_module,
        "bytearray",
        ExtendForbiddenBytearray,
        raising=False,
    )

    sent = asyncio.run(exercise())

    assert sent[0]["status"] == 413


def test_sync_api_applies_authenticated_matching_envelope(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        json=envelope_json(),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["result"]["sync_id"] == "sync-1"
    assert sync_service.applied == [envelope()]


def test_sync_api_rejects_missing_auth_and_project_mismatch(
    client: TestClient,
) -> None:
    unauthenticated = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        json=envelope_json(),
    )
    mismatch = client.post(
        "/v1/projects/project-2/sync",
        json=envelope_json(),
        headers=AUTH,
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == (
        "project_api_authentication_required"
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "sync_project_mismatch"


def test_sync_api_redacts_internal_and_invalid_payload_errors(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    invalid = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        content=b"not-json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    sync_service.apply_error = RuntimeError("provider-secret-message")
    failed = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        json=envelope_json(),
        headers=AUTH,
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "sync_invalid_request"
    assert failed.status_code == 500
    assert failed.json()["detail"] == "sync_internal_error"
    assert "provider-secret-message" not in failed.text


def test_sync_api_maps_known_sync_validation_error(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    sync_service.apply_error = ProjectSyncValidationError(
        "content_hash_mismatch"
    )
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/sync",
        json=envelope_json(),
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "content_hash_mismatch"


def test_sync_status_requires_project_access(client: TestClient) -> None:
    response = client.get(
        f"/v1/projects/{PROJECT_ID}/sync/status",
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["status"]["health"] == "healthy"


def retrieval_body() -> dict[str, object]:
    return {
        "request_id": "retrieval-1",
        "query_hash": digest("why plan B"),
        "source_refs": [source_ref().model_dump(mode="json")],
    }


def test_retrieval_request_create_list_and_get(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    created = client.post(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests",
        json=retrieval_body(),
        headers=AUTH,
    )
    listed = client.get(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests",
        headers=AUTH,
    )
    fetched = client.get(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests/retrieval-1",
        headers=AUTH,
    )
    assert created.status_code == 201
    assert created.json()["request"]["status"] == "pending"
    assert created.json()["request"]["source_id_hashes"] == [digest("doc-1")]
    assert len(listed.json()["requests"]) == 1
    assert listed.json()["requests"][0]["status"] == "in_progress"
    assert fetched.json()["request"] == listed.json()["requests"][0]
    assert sync_service.required_refs == [(source_ref(),)]


def test_retrieval_request_rejects_unregistered_source_summary(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    sync_service.source_error = ProjectSourceUnavailable("source_unavailable")
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests",
        json=retrieval_body(),
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "source_unavailable"


def test_retrieval_request_rejects_path_unsafe_request_id(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    body = retrieval_body()
    body["request_id"] = "a/b"

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests",
        json=body,
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "sync_invalid_request"
    assert sync_service.required_refs == []


def test_retrieval_request_rejects_hash_with_valid_substring(
    client: TestClient,
    sync_service: StubSyncService,
) -> None:
    body = retrieval_body()
    body["query_hash"] = "x" + "a" * 64 + "y"

    response = client.post(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests",
        json=body,
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "sync_invalid_request"
    assert sync_service.required_refs == []


def test_retrieval_request_has_no_independent_complete_mutation(
    client: TestClient,
) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT_ID}/retrieval-requests/retrieval-1/complete",
        headers=AUTH,
    )
    assert response.status_code == 404


def test_device_app_does_not_expose_sync_routes(tmp_path) -> None:
    app = create_app(settings(tmp_path / "device-api.db"))
    paths = {route.path for route in app.routes}
    assert f"/v1/projects/{{project_id}}/sync" not in paths
    assert f"/v1/projects/{{project_id}}/sync/status" not in paths
