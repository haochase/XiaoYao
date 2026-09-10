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
from companion_gateway.settings import Settings


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
TOKEN = "ops-owner-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


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

    def load_clock_state(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(clock_untrusted=self._clock_untrusted)

    def project_requires_clock_resync(self, project_id: str) -> bool:
        assert project_id == "project-1"
        return self._needs_sync


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
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    sync_repository = _SyncRepository()
    snapshot_reader = _SnapshotReader()
    app = create_app(
        Settings(database_path=database_path, project_api_principals=(principal,)),
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
