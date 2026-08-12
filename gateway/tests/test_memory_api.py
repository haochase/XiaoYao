from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.domain.memory import MemoryCategory, MemoryProposalCandidate
from companion_gateway.settings import Settings


@pytest.fixture
def enabled_client(tmp_path) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            database_path=tmp_path / "memory-api.db",
            memory_enabled=True,
        )
    )
    with TestClient(app) as client:
        yield client


def memory_payload(
    value: str = "morning reminders",
    *,
    subject_id: str = "family-1",
    confirmed: bool = True,
) -> dict[str, object]:
    return {
        "subject_id": subject_id,
        "category": "reminder_preference",
        "value": value,
        "confirmed": confirmed,
    }


def test_memory_api_is_disabled_by_default(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "disabled.db"))
    with TestClient(app) as client:
        response = client.get("/v1/memory", params={"subject_id": "family-1"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Memory feature is disabled"


def test_memory_api_requires_confirmation_and_does_not_write(enabled_client) -> None:
    response = enabled_client.post(
        "/v1/memory/confirm",
        headers={"X-Trace-Id": "trace-confirmation"},
        json=memory_payload(confirmed=False),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "memory consent required"
    assert enabled_client.get(
        "/v1/memory",
        params={"subject_id": "family-1"},
    ).json() == {"memories": []}


def test_memory_api_confirm_list_query_export_and_delete(enabled_client) -> None:
    response = enabled_client.post(
        "/v1/memory/confirm",
        headers={"X-Trace-Id": "trace-confirmed"},
        json=memory_payload(),
    )
    assert response.status_code == 200
    memory = response.json()["memory"]
    assert memory["source"] == "trace-confirmed"

    listed = enabled_client.get(
        "/v1/memory",
        params={"subject_id": "family-1", "query": "morning"},
    )
    assert listed.status_code == 200
    assert listed.json()["memories"][0]["memory_id"] == memory["memory_id"]

    exported = enabled_client.get(
        "/v1/memory/export",
        params={"subject_id": "family-1"},
    )
    assert exported.status_code == 200
    assert exported.json()["memories"] == listed.json()["memories"]

    deleted = enabled_client.delete(
        f"/v1/memory/{memory['memory_id']}",
        params={"subject_id": "family-1"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}


def test_memory_api_preserves_subject_isolation_and_validates_limit(enabled_client) -> None:
    created = enabled_client.post(
        "/v1/memory/confirm",
        json=memory_payload(),
    ).json()["memory"]

    wrong_subject = enabled_client.delete(
        f"/v1/memory/{created['memory_id']}",
        params={"subject_id": "family-2"},
    )
    assert wrong_subject.status_code == 404

    invalid_limit = enabled_client.get(
        "/v1/memory",
        params={"subject_id": "family-1", "limit": 0},
    )
    assert invalid_limit.status_code == 422

    still_present = enabled_client.get(
        "/v1/memory",
        params={"subject_id": "family-1"},
    )
    assert still_present.json()["memories"][0]["memory_id"] == created["memory_id"]


def test_memory_proposal_api_lists_confirms_and_consumes_pending_row(tmp_path) -> None:
    fixed_now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    app = create_app(
        Settings(
            database_path=tmp_path / "proposal-api.db",
            memory_enabled=True,
        ),
        memory_clock=lambda: fixed_now,
    )
    proposal = app.state.memory_service.propose(
        subject_id="family-1",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="Call me Chase",
            ),
        ),
        source="trace-model",
        now=fixed_now,
    )[0]

    with TestClient(app) as client:
        listed = client.get(
            "/v1/memory/proposals",
            params={"subject_id": "family-1"},
        )
        before_confirm = client.get(
            "/v1/memory",
            params={"subject_id": "family-1"},
        )
        confirmed = client.post(
            f"/v1/memory/proposals/{proposal.proposal_id}/confirm",
            headers={"X-Trace-Id": "trace-user-confirm"},
            json={"subject_id": "family-1"},
        )
        after_confirm = client.get(
            "/v1/memory/proposals",
            params={"subject_id": "family-1"},
        )

    assert listed.status_code == 200
    assert listed.json()["proposals"][0]["proposal_id"] == proposal.proposal_id
    assert before_confirm.json() == {"memories": []}
    assert confirmed.status_code == 200
    assert confirmed.json()["memory"]["source"] == "trace-user-confirm"
    assert confirmed.json()["memory"]["value"] == "Call me Chase"
    assert after_confirm.json() == {"proposals": []}


def test_memory_proposal_api_rejects_and_hides_cross_subject_access(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "proposal-isolation-api.db",
            memory_enabled=True,
        )
    )
    proposal = app.state.memory_service.propose(
        subject_id="family-2",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="Other user",
            ),
        ),
        source="trace-model",
    )[0]

    with TestClient(app) as client:
        mismatch = client.post(
            f"/v1/memory/proposals/{proposal.proposal_id}/confirm",
            json={"subject_id": "family-1"},
        )
        rejected = client.delete(
            f"/v1/memory/proposals/{proposal.proposal_id}",
            params={"subject_id": "family-2"},
        )
        missing = client.delete(
            f"/v1/memory/proposals/{proposal.proposal_id}",
            params={"subject_id": "family-2"},
        )

    assert mismatch.status_code == 404
    assert rejected.status_code == 200
    assert rejected.json() == {"deleted": True}
    assert missing.status_code == 404


def test_memory_proposal_api_is_disabled_by_default(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "proposal-disabled.db"))
    with TestClient(app) as client:
        listed = client.get(
            "/v1/memory/proposals",
            params={"subject_id": "family-1"},
        )
        confirmed = client.post(
            "/v1/memory/proposals/prop-1/confirm",
            json={"subject_id": "family-1"},
        )

    assert listed.status_code == 503
    assert confirmed.status_code == 503
