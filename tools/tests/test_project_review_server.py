from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tools.project_review_server import create_review_app


TOKEN = b"review-token"
PROJECT_ID = "project-fixed"
SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "project_review_server.py"


class _Protector:
    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        assert project_id == PROJECT_ID
        assert protected == b"protected"
        return TOKEN


def test_review_server_imports_when_run_outside_the_repository(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy; "
                f"runpy.run_path({str(SERVER_SCRIPT)!r}, run_name='review_server_probe')"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class _Response:
    status = 200

    def __init__(self, url: str, payload: dict[str, Any]) -> None:
        self._url = url
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, _size: int) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir()
    policy = {
        "reviewer": {
            "token_sha256": sha256(TOKEN).hexdigest(),
            "project_ids": [PROJECT_ID],
            "permission_scopes": ["project:fixed"],
            "can_review": True,
        }
    }
    (root / "principal.json").write_text(json.dumps(policy), encoding="utf-8")
    (root / "credential.dpapi").write_bytes(b"protected")
    return root


def _opener(calls: list[tuple[str, str, bytes | None]]):
    def open_request(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 5.0
        calls.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                request.data,
            )
        )
        if request.full_url.endswith("/ops/summary"):
            return _Response(
                request.full_url,
                {
                    "project_name": "固定项目",
                    "source_count": 2,
                    "freshness_seconds": 300,
                    "last_success_at_present": True,
                    "clock_status": "normal",
                    "active_decision_count": 1,
                    "proposed_candidate_count": 1,
                    "rejected_candidate_count": 0,
                    "accepted_candidate_count": 0,
                },
            )
        if request.full_url.endswith("/ops/conflicts"):
            return _Response(
                request.full_url,
                {
                    "conflicts": [
                        {
                            "candidate_id": "candidate-1",
                            "decision_id": "decision-1",
                            "active_text": "当前方案",
                            "proposed_text": "候选方案",
                            "reason": "需要确认",
                            "status": "proposed",
                            "created_at": "2026-09-10T08:00:00+00:00",
                            "reviewed_by": None,
                            "reviewed_at": None,
                            "review_reason": None,
                            "evidence": None,
                        }
                    ]
                },
            )
        return _Response(
            request.full_url,
            {
                "candidate": {"candidate_id": "candidate-1", "status": "accepted"},
                "version": {"version": 2},
            },
        )

    return open_request


def test_review_bff_serves_page_and_proxies_the_policy_project(tmp_path: Path) -> None:
    calls: list[tuple[str, str, bytes | None]] = []
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener(calls),
    )
    client = TestClient(app, headers={"Host": "127.0.0.1:8724"})

    page = client.get("/")
    summary = client.get("/api/summary")
    conflicts = client.get("/api/conflicts")

    assert page.status_code == 200
    assert "/api/summary" in page.text
    assert "/v1/projects/" not in page.text
    assert PROJECT_ID not in page.text
    assert summary.json()["source_count"] == 2
    assert conflicts.json()["conflicts"][0]["candidate_id"] == "candidate-1"
    assert calls == [
        (
            f"http://127.0.0.1:8723/v1/projects/{PROJECT_ID}/ops/summary",
            "Bearer review-token",
            None,
        ),
        (
            f"http://127.0.0.1:8723/v1/projects/{PROJECT_ID}/ops/conflicts",
            "Bearer review-token",
            None,
        ),
    ]


def test_review_bff_rejects_nonlocal_host_and_cross_origin_post(tmp_path: Path) -> None:
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener([]),
    )
    client = TestClient(app)

    assert client.get("/api/summary", headers={"Host": "example.invalid"}).status_code == 400
    assert client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Host": "localhost:8724", "Origin": "http://example.invalid"},
        json={"action": "accept", "change_reason": "确认"},
    ).status_code == 403


def test_review_bff_accepts_only_fixed_review_json(tmp_path: Path) -> None:
    calls: list[tuple[str, str, bytes | None]] = []
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener(calls),
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    assert client.post(
        "/api/conflicts/not%20an%20id/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": "accept", "change_reason": "确认"},
    ).status_code == 422
    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": "accept", "change_reason": "确认", "reviewer_id": "forged"},
    )

    assert response.status_code == 422
    assert calls == []


def test_review_bff_proxies_a_valid_review_without_exposing_upstream_data(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, bytes | None]] = []
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener(calls),
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": "accept", "change_reason": "确认"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "candidate_id": "candidate-1",
        "status": "accepted",
        "version": 2,
    }
    assert calls == [
        (
            "http://127.0.0.1:8723/v1/projects/conflicts/candidate-1/review",
            "Bearer review-token",
            b'{"action": "accept", "change_reason": "\\u786e\\u8ba4"}',
        )
    ]


def test_review_bff_accepts_a_rejected_candidate_without_a_version(tmp_path: Path) -> None:
    def reject_opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 5.0
        return _Response(
            request.full_url,
            {"candidate": {"candidate_id": "candidate-1", "status": "rejected"}},
        )

    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=reject_opener,
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": "reject", "change_reason": "保留原决策"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "candidate_id": "candidate-1",
        "status": "rejected",
    }


_MISSING = object()


@pytest.mark.parametrize(
    ("action", "candidate", "version"),
    [
        pytest.param(
            "accept",
            {"candidate_id": "candidate-other", "status": "accepted"},
            {"version": 2},
            id="candidate-id-mismatch",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "rejected"},
            {"version": 2},
            id="accept-status-mismatch",
        ),
        pytest.param(
            "reject",
            {"candidate_id": "candidate-1", "status": "accepted"},
            _MISSING,
            id="reject-status-mismatch",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            _MISSING,
            id="accept-version-missing",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            {},
            id="accept-version-value-missing",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            {"version": False},
            id="accept-version-false",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            {"version": True},
            id="accept-version-true",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            {"version": "2"},
            id="accept-version-string",
        ),
        pytest.param(
            "accept",
            {"candidate_id": "candidate-1", "status": "accepted"},
            {"version": 2.0},
            id="accept-version-float",
        ),
        pytest.param(
            "reject",
            {"candidate_id": "candidate-1", "status": "rejected"},
            None,
            id="reject-version-null",
        ),
        pytest.param(
            "reject",
            {"candidate_id": "candidate-1", "status": "rejected"},
            {"version": 2},
            id="reject-version-object",
        ),
    ],
)
def test_review_bff_fails_closed_for_invalid_upstream_review_responses(
    tmp_path: Path,
    action: str,
    candidate: dict[str, Any],
    version: object,
) -> None:
    def response_opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 5.0
        payload: dict[str, Any] = {"candidate": candidate}
        if version is not _MISSING:
            payload["version"] = version
        return _Response(request.full_url, payload)

    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=response_opener,
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": action, "change_reason": "审核"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "review_service_unavailable"}


@pytest.mark.parametrize("action", [None, True, False, "approve", ""])
def test_review_bff_rejects_invalid_actions_before_proxying(
    tmp_path: Path, action: object
) -> None:
    calls: list[tuple[str, str, bytes | None]] = []
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener(calls),
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724"},
        json={"action": action, "change_reason": "审核"},
    )

    assert response.status_code == 422
    assert calls == []


def test_review_bff_rejects_malformed_json_before_proxying(tmp_path: Path) -> None:
    calls: list[tuple[str, str, bytes | None]] = []
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener(calls),
    )
    client = TestClient(
        app,
        headers={"Host": "localhost:8724"},
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/conflicts/candidate-1/review",
        headers={"Origin": "http://localhost:8724", "Content-Type": "application/json"},
        content=b"{",
    )

    assert response.status_code == 422
    assert calls == []


class _SessionController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, object | None]] = []

    @staticmethod
    def _result(status: str) -> SimpleNamespace:
        return SimpleNamespace(
            to_dict=lambda: {
                "status": status,
                "started_at": "2026-09-11T09:00:00+00:00",
                "expires_at": "2026-09-11T11:00:00+00:00",
                "next_due_at": "2026-09-11T09:05:00+00:00",
                "stage": "end",
                "error_type": None,
                "release_status": None,
                "token": "must-not-leak",
            }
        )

    def session_status(self, root: Path) -> SimpleNamespace:
        self.calls.append(("status", root, None))
        return self._result("active")

    def start_session(self, root: Path, protector: object) -> SimpleNamespace:
        self.calls.append(("start", root, protector))
        return self._result("active")

    def stop_session(self, root: Path) -> SimpleNamespace:
        self.calls.append(("stop", root, None))
        return self._result("stopped")

    def resume_session(self, root: Path) -> SimpleNamespace:
        self.calls.append(("resume", root, None))
        return self._result("active")


def test_review_bff_exposes_only_sanitized_session_state_and_actions(
    tmp_path: Path,
) -> None:
    controller = _SessionController()
    protector = _Protector()
    session_root = tmp_path / "session-root"
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=protector,
        opener=_opener([]),
        session_root=session_root,
        session_controller=controller,
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})
    origin = {"Origin": "http://localhost:8724"}

    responses = [
        client.get("/api/session"),
        client.post("/api/session/start", headers=origin),
        client.post("/api/session/stop", headers=origin),
        client.post("/api/session/resume", headers=origin),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert [response.json()["status"] for response in responses] == [
        "active",
        "active",
        "stopped",
        "active",
    ]
    assert all("token" not in response.json() for response in responses)
    assert controller.calls == [
        ("status", session_root, None),
        ("start", session_root, protector),
        ("stop", session_root, None),
        ("resume", session_root, None),
    ]


def test_review_bff_rejects_cross_origin_session_actions(tmp_path: Path) -> None:
    controller = _SessionController()
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener([]),
        session_root=tmp_path / "session-root",
        session_controller=controller,
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.post(
        "/api/session/start",
        headers={"Origin": "http://example.invalid"},
    )

    assert response.status_code == 403
    assert controller.calls == []


def test_review_bff_fails_closed_for_invalid_session_result(tmp_path: Path) -> None:
    controller = _SessionController()
    controller.session_status = lambda _root: SimpleNamespace(  # type: ignore[method-assign]
        to_dict=lambda: {"status": "unknown", "token": "must-not-leak"}
    )
    app = create_review_app(
        private_root=_private_root(tmp_path),
        protector=_Protector(),
        opener=_opener([]),
        session_root=tmp_path / "session-root",
        session_controller=controller,
    )
    client = TestClient(app, headers={"Host": "localhost:8724"})

    response = client.get("/api/session")

    assert response.status_code == 503
    assert response.json() == {"detail": "review_service_unavailable"}
