from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.project.models import AnswerKind
from companion_gateway.settings import Settings


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


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
        Settings(database_path=tmp_path / "project-api.db"),
        project_clock=lambda: clock["now"],
    )
    app.state.project_test_clock = clock
    with TestClient(app) as test_client:
        yield test_client


def test_project_context_and_fact_query_return_a_source(client: TestClient) -> None:
    stored = client.post("/v1/projects/project-1/context", json=context())

    assert stored.status_code == 201
    assert stored.json()["context"]["project_id"] == "project-1"

    answer = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": AnswerKind.FACT.value},
    )

    assert answer.status_code == 200
    assert answer.json()["answer"]["text"] == "采用方案 B"
    assert answer.json()["answer"]["source_refs"][0]["source_id"] == "meeting-1"


def test_project_query_rejects_expired_context(client: TestClient) -> None:
    client.post("/v1/projects/project-1/context", json=context())
    client.app.state.project_test_clock["now"] = NOW + timedelta(seconds=301)

    response = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": "fact"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "context_expired"


def test_project_conflict_review_accepts_new_decision_and_returns_version(
    client: TestClient,
) -> None:
    client.post("/v1/projects/project-1/context", json=context())
    proposal = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
    )

    assert proposal.status_code == 201
    candidate_id = proposal.json()["candidate"]["candidate_id"]

    review = client.post(
        f"/v1/projects/conflicts/{candidate_id}/review",
        json={
            "reviewer_id": "owner-1",
            "action": "accept",
            "new_decision_text": "采用方案 A",
            "change_reason": "供应商交期发生变化",
            "evidence_refs": [source("meeting-2")],
        },
    )

    assert review.status_code == 200
    assert review.json()["candidate"]["status"] == "accepted"
    assert review.json()["version"]["version"] == 2

    answer = client.post(
        "/v1/projects/project-1/query",
        json={"query": "终端方案", "kind": "decision_check"},
    )
    assert answer.json()["answer"]["text"] == "当前有效决策：采用方案 A"


def test_project_conflict_rejects_foreign_source_scope(client: TestClient) -> None:
    client.post("/v1/projects/project-1/context", json=context())
    response = client.post(
        "/v1/projects/project-1/conflicts",
        json={
            "decision_id": "decision-1",
            "observed_text": "改用方案 A",
            "reason": "来源不属于当前项目",
            "evidence_refs": [source("meeting-foreign", "project:other")],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "source_scope_mismatch"
