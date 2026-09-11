from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.project.auth import ProjectApiPrincipal
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.service import ProjectMemoryService
from companion_gateway.project.sync_models import SourceSyncStatus, SyncSourceType
from companion_gateway.project.sync_repository import SyncConflict
from companion_gateway.settings import Settings


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
TOKEN = "ops-owner-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
VIEWER_TOKEN = "ops-viewer-token"
VIEWER_HEADERS = {"Authorization": f"Bearer {VIEWER_TOKEN}"}


class _SnapshotReader:
    def __init__(self) -> None:
        self.raise_error = False
        self.source_status = SourceSyncStatus.ACTIVE

    def get(self, project_id: str):  # type: ignore[no-untyped-def]
        assert project_id == "project-1"
        if self.raise_error:
            raise RuntimeError("snapshot_unavailable")
        return SimpleNamespace(
            sources=(
                SimpleNamespace(
                    source_type=SyncSourceType.MEETING_NOTE,
                    source_id_hash="source-1",
                ),
            ),
            source_states=(
                SimpleNamespace(
                    source_type=SyncSourceType.MEETING_NOTE,
                    source_id_hash="source-1",
                    status=self.source_status,
                    last_success_at=NOW,
                ),
            ),
        )


class _SyncRepository:
    def __init__(self, *, clock_untrusted: bool = False, needs_sync: bool = False) -> None:
        self._clock_untrusted = clock_untrusted
        self._needs_sync = needs_sync
        self.preview_error: Exception | None = None
        self.apply_error: Exception | None = None
        self.apply_calls: list[dict[str, object]] = []
        self.preview = {
            "project_id": "project-1",
            "active_generation_id": "generation-2",
            "legacy": {
                "decision_id": "decision-1",
                "topic": "终端组合方案",
                "version": 2,
            },
            "retirement_version": 3,
            "replacements": [
                {
                    "decision_id": "decision-split-1",
                    "topic": "硬件方案",
                    "version": 1,
                },
                {
                    "decision_id": "decision-split-2",
                    "topic": "软件方案",
                    "version": 1,
                },
            ],
            "before_context_hash": "a" * 64,
            "after_context_hash": "b" * 64,
            "precondition_token": "c" * 64,
        }

    def load_clock_state(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(clock_untrusted=self._clock_untrusted)

    def project_requires_clock_resync(self, project_id: str) -> bool:
        assert project_id == "project-1"
        return self._needs_sync

    def preview_decision_split_migration(  # type: ignore[no-untyped-def]
        self,
        project_id: str,
    ):
        assert project_id == "project-1"
        if self.preview_error is not None:
            raise self.preview_error
        return self.preview

    def apply_decision_split_migration(self, **kwargs):  # type: ignore[no-untyped-def]
        if self.apply_error is not None:
            raise self.apply_error
        self.apply_calls.append(kwargs)
        return {
            "migration_id": "migration-1",
            "project_id": "project-1",
            "legacy_decision_id": "decision-1",
            "base_version": 2,
            "retirement_version": 3,
            "active_generation_id": "generation-2",
            "reviewer_id": kwargs["reviewer_id"],
            "migrated_at": kwargs["migrated_at"],
            "reason": kwargs["reason"],
            "replacement_decision_ids": [
                "decision-split-1",
                "decision-split-2",
            ],
            "before_context_hash": "a" * 64,
            "after_context_hash": "b" * 64,
        }


def _source() -> dict[str, object]:
    return {
        "source_type": "meeting_note",
        "source_id": "meeting-1",
        "source_title": "方案评审会",
        "source_url": "https://private.invalid/meeting-1",
        "source_time": NOW.isoformat(),
        "excerpt": "会议决定采用方案 B。",
        "permission_scope": "project:demo",
    }


def _context(project_id: str = "project-1") -> dict[str, object]:
    return {
        "project_id": project_id,
        "project_name": "演示项目",
        "generated_at": NOW.isoformat(),
        "source_refs": [],
        "active_decisions": [
            {
                "decision_id": "decision-1",
                "project_id": project_id,
                "topic": "终端方案",
                "decision_text": "采用方案 B",
                "rationale": "交付风险更低",
                "owner": "owner-1",
                "decided_at": NOW.isoformat(),
                "source_refs": [_source()],
                "status": "active",
                "confidence": 0.9,
            }
        ],
        "open_actions": [],
        "current_risks": [],
        "permission_scope": "project:demo",
        "freshness_seconds": 300,
    }


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    database_path = tmp_path / "ops.db"
    principal = ProjectApiPrincipal(
        principal_id="ops-owner",
        token_sha256=sha256(TOKEN.encode()).hexdigest(),
        project_ids=frozenset({"project-1"}),
        permission_scopes=frozenset({"project:demo"}),
        can_review=True,
    )
    viewer = ProjectApiPrincipal(
        principal_id="ops-viewer",
        token_sha256=sha256(VIEWER_TOKEN.encode()).hexdigest(),
        project_ids=frozenset({"project-1"}),
        permission_scopes=frozenset({"project:demo"}),
        can_review=False,
    )
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    sync_repository = _SyncRepository()
    snapshot_reader = _SnapshotReader()
    app = create_app(
        Settings(
            database_path=database_path,
            project_api_principals=(principal, viewer),
        ),
        project_memory_service=service,
        project_clock=lambda: NOW,
        project_ops_snapshot_reader=snapshot_reader,
        project_ops_sync_repository=sync_repository,
    )
    app.state.project_ops_sync_repository = sync_repository
    app.state.project_ops_snapshot_reader = snapshot_reader
    with TestClient(app) as test_client:
        test_client.post("/v1/projects/project-1/context", json=_context(), headers=HEADERS)
        yield test_client


def _create_candidate(client: TestClient) -> str:
    response = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "proposed_decision_text": "采用方案 A",
            "reason": "供应商交期变化",
            "evidence_refs": [_source()],
        },
        headers=HEADERS,
    )
    assert response.status_code == 201
    return response.json()["candidate"]["candidate_id"]


def test_ops_summary_is_authenticated_and_redacted(client: TestClient) -> None:
    assert client.get("/v1/projects/project-1/ops/summary").status_code == 401
    response = client.get("/v1/projects/project-1/ops/summary", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["project_name"] == "演示项目"
    assert body["source_count"] == 1
    assert "source_id" not in body
    assert "source_url" not in body
    assert "token" not in str(body).lower()


def test_ops_summary_rejects_cross_project_and_counts_decision_sources(
    client: TestClient,
) -> None:
    response = client.get("/v1/projects/project-2/ops/summary", headers=HEADERS)
    assert response.status_code == 403


def test_ops_summary_prioritizes_untrusted_clock(client: TestClient) -> None:
    sync_repository = client.app.state.project_ops_sync_repository
    sync_repository._clock_untrusted = True
    sync_repository._needs_sync = True

    response = client.get("/v1/projects/project-1/ops/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["clock_status"] == "clock_untrusted"


def test_ops_summary_keeps_untrusted_clock_when_snapshot_is_unavailable(
    client: TestClient,
) -> None:
    client.app.state.project_ops_sync_repository._clock_untrusted = True
    client.app.state.project_ops_snapshot_reader.raise_error = True

    response = client.get("/v1/projects/project-1/ops/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["clock_status"] == "clock_untrusted"


@pytest.mark.parametrize("status", [SourceSyncStatus.STALE, SourceSyncStatus.FAILED])
def test_ops_summary_keeps_current_sources_with_historical_success(
    client: TestClient,
    status: SourceSyncStatus,
) -> None:
    client.app.state.project_ops_snapshot_reader.source_status = status

    response = client.get("/v1/projects/project-1/ops/summary", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["source_count"] == 1
    assert response.json()["last_success_at_present"] is True


def test_ops_conflicts_redact_source_identity_and_limit_excerpt(client: TestClient) -> None:
    candidate_id = _create_candidate(client)
    response = client.get("/v1/projects/project-1/ops/conflicts", headers=HEADERS)
    assert response.status_code == 200
    candidate = response.json()["conflicts"][0]
    assert candidate["candidate_id"] == candidate_id
    assert "source_id" not in str(candidate)
    assert "source_url" not in str(candidate)
    assert "observed_text" not in candidate
    assert len(candidate.get("evidence", {}).get("excerpt", "")) <= 150


def test_ops_review_uses_server_reviewer_and_returns_version(client: TestClient) -> None:
    candidate_id = _create_candidate(client)
    response = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={"action": "reject", "change_reason": "保留原决策", "reviewer_id": "evil"},
        headers=HEADERS,
    )
    assert response.status_code == 422
    response = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={"action": "reject", "change_reason": "保留原决策"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["candidate"]["reviewed_by"] == "ops-owner"


def test_decision_split_preview_requires_reviewer_and_is_redacted(
    client: TestClient,
) -> None:
    path = "/v1/projects/project-1/ops/decision-split-migration"

    missing = client.get(path)
    forbidden = client.get(path, headers=VIEWER_HEADERS)
    response = client.get(path, headers=HEADERS)

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert response.status_code == 200
    assert response.json() == client.app.state.project_ops_sync_repository.preview
    serialized = response.text
    assert "decision_text" not in serialized
    assert "source_id" not in serialized
    assert "reviewer_id" not in serialized


def test_decision_split_apply_uses_server_reviewer_and_clock(
    client: TestClient,
) -> None:
    path = "/v1/projects/project-1/decision-split-migrations"
    body = {
        "precondition_token": "c" * 64,
        "reason": "拆分组合决策",
        "confirmed": True,
    }

    missing = client.post(path, json=body)
    forbidden = client.post(path, json=body, headers=VIEWER_HEADERS)
    response = client.post(path, json=body, headers=HEADERS)

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert response.status_code == 200
    repository = client.app.state.project_ops_sync_repository
    assert repository.apply_calls == [
        {
            "project_id": "project-1",
            "precondition_token": "c" * 64,
            "reviewer_id": "ops-owner",
            "reason": "拆分组合决策",
            "migrated_at": NOW,
        }
    ]
    assert response.json()["migration"]["reviewer_id"] == "ops-owner"
    assert response.json()["migration"]["migrated_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    "body",
    [
        {
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": False,
        },
        {
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": 1,
        },
        {
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": True,
            "reviewer_id": "evil",
        },
        {
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": True,
            "legacy_decision_id": "decision-1",
        },
        {
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": True,
            "migrated_at": NOW.isoformat(),
        },
    ],
)
def test_decision_split_apply_rejects_unconfirmed_or_open_body(
    client: TestClient,
    body: dict[str, object],
) -> None:
    response = client.post(
        "/v1/projects/project-1/decision-split-migrations",
        json=body,
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert client.app.state.project_ops_sync_repository.apply_calls == []


def test_decision_split_endpoints_map_state_conflict_to_409(
    client: TestClient,
) -> None:
    repository = client.app.state.project_ops_sync_repository
    repository.preview_error = SyncConflict("decision_split_candidate_unavailable")
    preview = client.get(
        "/v1/projects/project-1/ops/decision-split-migration",
        headers=HEADERS,
    )
    repository.preview_error = None
    repository.apply_error = SyncConflict("decision_split_precondition_stale")
    applied = client.post(
        "/v1/projects/project-1/decision-split-migrations",
        json={
            "precondition_token": "c" * 64,
            "reason": "拆分组合决策",
            "confirmed": True,
        },
        headers=HEADERS,
    )

    assert preview.status_code == 409
    assert preview.json() == {"detail": "decision_split_candidate_unavailable"}
    assert applied.status_code == 409
    assert applied.json() == {"detail": "decision_split_precondition_stale"}


def test_ops_page_is_local_and_does_not_handle_tokens(client: TestClient) -> None:
    response = client.get("/project/")
    assert response.status_code == 404
    html = (Path(__file__).parents[1] / "static" / "project" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/api/summary" in html
    assert "/v1/projects/" not in html
    assert "localStorage" not in html
    assert "<script src=\"http" not in html
    assert "暂无待确认项" in html
    assert "window.confirm" not in html
    assert "confirmReview" in html
    assert '"取消"' in html
    assert 'aria-busy' in html
    assert 'dataset.submitting' in html
