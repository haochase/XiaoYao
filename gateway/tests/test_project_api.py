from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.project.models import AnswerKind
from companion_gateway.project.auth import ProjectApiPrincipal
from companion_gateway.settings import Settings


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
OWNER_TOKEN = "owner-project-token"
OTHER_TOKEN = "other-project-token"
VIEWER_TOKEN = "viewer-project-token"
WRONG_SCOPE_TOKEN = "wrong-scope-project-token"
OWNER_HEADERS = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def project_settings(database_path) -> Settings:
    return Settings(
        database_path=database_path,
        project_api_principals=(
            ProjectApiPrincipal(
                principal_id="owner-1",
                token_sha256=sha256(OWNER_TOKEN.encode()).hexdigest(),
                project_ids=frozenset({"project-1"}),
                permission_scopes=frozenset({"project:star-retail"}),
                can_review=True,
            ),
            ProjectApiPrincipal(
                principal_id="owner-2",
                token_sha256=sha256(OTHER_TOKEN.encode()).hexdigest(),
                project_ids=frozenset({"project-2"}),
                permission_scopes=frozenset({"project:other"}),
                can_review=True,
            ),
            ProjectApiPrincipal(
                principal_id="viewer-1",
                token_sha256=sha256(VIEWER_TOKEN.encode()).hexdigest(),
                project_ids=frozenset({"project-1"}),
                permission_scopes=frozenset({"project:star-retail"}),
                can_review=False,
            ),
            ProjectApiPrincipal(
                principal_id="scope-owner",
                token_sha256=sha256(WRONG_SCOPE_TOKEN.encode()).hexdigest(),
                project_ids=frozenset({"project-1"}),
                permission_scopes=frozenset({"project:other"}),
                can_review=True,
            ),
        ),
    )


def source(source_id: str = "meeting-1", scope: str = "project:star-retail") -> dict[str, object]:
    return {
        "source_type": "meeting_note",
        "source_id": source_id,
        "source_title": "方案评审会",
        "source_url": f"https://example.invalid/{source_id}",
        "source_time": NOW.isoformat(),
        "excerpt": "会议决定采用方案 B。",
        "permission_scope": scope,
    }


def decision(text: str = "采用方案 B") -> dict[str, object]:
    return {
        "decision_id": "decision-1",
        "project_id": "project-1",
        "topic": "终端方案",
        "decision_text": text,
        "rationale": "交付风险更低",
        "owner": "owner-1",
        "decided_at": NOW.isoformat(),
        "source_refs": [source()],
        "status": "active",
        "confidence": 0.92,
    }


def context(*, generated_at: datetime = NOW) -> dict[str, object]:
    return {
        "project_id": "project-1",
        "project_name": "星河零售终端升级项目",
        "generated_at": generated_at.isoformat(),
        "source_refs": [source()],
        "active_decisions": [decision()],
        "open_actions": ["补充方案 B 的成本测算"],
        "current_risks": ["供应商交期未确认"],
        "next_meeting": "2026-09-05 10:00",
        "permission_scope": "project:star-retail",
        "freshness_seconds": 300,
    }


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    clock = {"now": NOW}
    app = create_app(
        project_settings(tmp_path / "project-api.db"),
        project_clock=lambda: clock["now"],
    )
    app.state.project_test_clock = clock
    with TestClient(app) as test_client:
        yield test_client


def test_project_context_and_fact_query_return_a_source(client: TestClient) -> None:
    stored = client.post(
        "/v1/projects/project-1/context",
        json=context(),
        headers=OWNER_HEADERS,
    )

    assert stored.status_code == 201
    assert stored.json()["context"]["project_id"] == "project-1"

    answer = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": AnswerKind.FACT.value},
        headers=OWNER_HEADERS,
    )

    assert answer.status_code == 200
    assert answer.json()["answer"]["text"] == "采用方案 B"
    assert answer.json()["answer"]["source_refs"][0]["source_id"] == "meeting-1"


def test_project_query_rejects_expired_context(client: TestClient) -> None:
    client.post(
        "/v1/projects/project-1/context", json=context(), headers=OWNER_HEADERS
    )
    client.app.state.project_test_clock["now"] = NOW + timedelta(seconds=301)

    response = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": "fact"},
        headers=OWNER_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "context_expired"


def test_project_conflict_review_accepts_new_decision_and_returns_version(
    client: TestClient,
) -> None:
    client.post(
        "/v1/projects/project-1/context", json=context(), headers=OWNER_HEADERS
    )
    proposal = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
        headers=OWNER_HEADERS,
    )

    assert proposal.status_code == 201
    candidate_id = proposal.json()["candidate"]["candidate_id"]

    review = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={
            "action": "accept",
            "new_decision_text": "采用方案 A",
            "change_reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
        headers=OWNER_HEADERS,
    )

    assert review.status_code == 200
    assert review.json()["candidate"]["status"] == "accepted"
    assert review.json()["version"]["version"] == 2

    answer = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": "decision_check"},
        headers=OWNER_HEADERS,
    )
    assert answer.json()["answer"]["text"] == "当前有效决策：采用方案 A"


def test_project_conflict_rejects_foreign_source_scope(client: TestClient) -> None:
    client.post(
        "/v1/projects/project-1/context", json=context(), headers=OWNER_HEADERS
    )
    response = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "来源不属于当前项目",
            "evidence_refs": [source("meeting-foreign", "project:other")],
        },
        headers=OWNER_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "source_scope_mismatch"


def test_project_context_survives_app_recreation(tmp_path) -> None:
    database_path = tmp_path / "project-restart.db"
    first_app = create_app(
        project_settings(database_path),
        project_clock=lambda: NOW,
    )
    with TestClient(first_app) as first_client:
        assert (
            first_client.post(
                "/v1/projects/project-1/context",
                json=context(),
                headers=OWNER_HEADERS,
            ).status_code
            == 201
        )

    second_app = create_app(
        project_settings(database_path),
        project_clock=lambda: NOW,
    )
    with TestClient(second_app) as second_client:
        response = second_client.post(
            "/v1/projects/project-1/query",
            json={"query": "终端方案", "kind": "fact"},
            headers=OWNER_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["answer"]["text"] == "采用方案 B"


def test_project_api_rejects_missing_or_cross_project_credentials(
    client: TestClient,
) -> None:
    missing = client.post("/v1/projects/project-1/context", json=context())
    cross_project = client.post(
        "/v1/projects/project-1/context",
        json=context(),
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )

    assert missing.status_code == 401
    assert cross_project.status_code == 403


def test_project_review_uses_authenticated_principal_and_requires_review_role(
    client: TestClient,
) -> None:
    client.post(
        "/v1/projects/project-1/context", json=context(), headers=OWNER_HEADERS
    )
    proposal = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
        headers=OWNER_HEADERS,
    )
    candidate_id = proposal.json()["candidate"]["candidate_id"]

    forbidden = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={"action": "reject", "change_reason": "缺少审批权"},
        headers={"Authorization": f"Bearer {VIEWER_TOKEN}"},
    )
    reviewed = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={"action": "reject", "change_reason": "保留原决策"},
        headers=OWNER_HEADERS,
    )

    assert forbidden.status_code == 403
    assert reviewed.status_code == 200
    assert reviewed.json()["candidate"]["reviewed_by"] == "owner-1"


def test_project_api_rejects_same_project_principal_without_context_scope(
    client: TestClient,
) -> None:
    wrong_scope_headers = {"Authorization": f"Bearer {WRONG_SCOPE_TOKEN}"}
    client.post(
        "/v1/projects/project-1/context", json=context(), headers=OWNER_HEADERS
    )
    proposal = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
        headers=OWNER_HEADERS,
    )
    candidate_id = proposal.json()["candidate"]["candidate_id"]

    query = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": "fact"},
        headers=wrong_scope_headers,
    )
    conflict = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 C",
            "reason": "权限域不匹配",
            "evidence_refs": [source("meeting-3")],
        },
        headers=wrong_scope_headers,
    )
    review = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={"action": "reject", "change_reason": "权限域不匹配"},
        headers=wrong_scope_headers,
    )

    assert query.status_code == 403
    assert conflict.status_code == 403
    assert review.status_code == 403
    assert query.json()["detail"] == "project_scope_denied"
