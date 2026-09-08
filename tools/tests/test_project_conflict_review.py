from __future__ import annotations

import hashlib
import json
import stat
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from tools import project_conflict_review as review_cli


TOKEN = "review-token-that-must-stay-private"
PROJECT_ID = "project-1"
PERMISSION_SCOPE = "project:star-retail"


class FixtureProtector:
    def __init__(self, *, plaintext: bytes | None = None) -> None:
        self.plaintext = TOKEN.encode() if plaintext is None else plaintext
        self.calls: list[tuple[str, bytes]] = []

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        self.calls.append((project_id, protected))
        return self.plaintext


class FailingProtector:
    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        raise RuntimeError("DPAPI details must stay private")


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, object] | bytes,
        *,
        url: str,
        status: int = 200,
    ) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self._stream = BytesIO(raw)
        self._url = url
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def candidate(
    candidate_id: str = "candidate-1",
    *,
    status: str = "proposed",
) -> dict[str, object]:
    reviewed = status != "proposed"
    return {
        "candidate_id": candidate_id,
        "project_id": PROJECT_ID,
        "decision_id": "decision-1",
        "base_version": 1,
        "observed_text": "把桌面终端切换为树莓派",
        "proposed_decision_text": "采用树莓派作为桌面终端",
        "active_decision_text": "采用 ESP32-S3 作为桌面终端",
        "reason": "会议发言与当前有效决策不一致",
        "source_refs": [
            {
                "source_type": "meeting_note",
                "source_id": "meeting-1",
                "source_title": "方案评审会",
                "source_url": "https://example.invalid/meeting-1",
                "source_time": "2026-09-08T08:00:00Z",
                "excerpt": "当前有效决策为 ESP32-S3。",
                "permission_scope": PERMISSION_SCOPE,
            }
        ],
        "active_approval_ref": None,
        "status": status,
        "created_at": "2026-09-08T08:00:00Z",
        "reviewed_by": "owner-1" if reviewed else None,
        "reviewed_at": "2026-09-08T08:01:00Z" if reviewed else None,
        "review_reason": "已审核" if reviewed else None,
    }


def write_review_fixture(
    tmp_path: Path,
    *,
    policy: dict[str, object] | None = None,
) -> Path:
    root = tmp_path / "project-review"
    root.mkdir()
    selected_policy = policy if policy is not None else {
        "owner-1": {
            "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
            "project_ids": [PROJECT_ID],
            "permission_scopes": [PERMISSION_SCOPE],
            "can_review": True,
        }
    }
    (root / "principal.json").write_text(
        json.dumps(selected_policy), encoding="utf-8"
    )
    (root / "credential.dpapi").write_bytes(b"protected-token")
    return root


def recording_opener(
    requests: list[object],
    *,
    proposed: list[dict[str, object]] | None = None,
    post_status: str | None = None,
):
    selected = [candidate()] if proposed is None else proposed

    def open_request(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == review_cli.REQUEST_TIMEOUT_SECONDS
        requests.append(request)
        if request.method == "GET":
            return FakeResponse(
                {"conflicts": selected},
                url=request.full_url,
            )
        body = json.loads(request.data)
        response_status = post_status or (
            "accepted" if body["action"] == "accept" else "rejected"
        )
        return FakeResponse(
            {
                "candidate": candidate(
                    selected[0]["candidate_id"],
                    status=response_status,
                )
            },
            url=request.full_url,
        )

    return open_request


def assert_public_output_is_redacted(output: str) -> dict[str, object]:
    public = json.loads(output)
    serialized = json.dumps(public, ensure_ascii=False)
    for secret in (
        TOKEN,
        "candidate-1",
        "把桌面终端切换为树莓派",
        "采用树莓派作为桌面终端",
        "meeting-1",
    ):
        assert secret not in serialized
    return public


def test_accept_uses_only_unique_proposed_candidate_and_protected_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []
    protector = FixtureProtector()

    result = review_cli.main(
        ["accept", "--reason", "负责人确认采用会议候选方案"],
        private_root=private_root,
        protector=protector,
        opener=recording_opener(requests),
    )

    assert result == 0
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[0].full_url == (
        "http://127.0.0.1:8724/v1/projects/project-1/conflicts?status=proposed"
    )
    assert requests[1].full_url == (
        "http://127.0.0.1:8724/v1/projects/conflicts/candidate-1/review"
    )
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert requests[1].headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(requests[1].data) == {
        "action": "accept",
        "change_reason": "负责人确认采用会议候选方案",
    }
    assert protector.calls == [(PROJECT_ID, b"protected-token")]
    public = assert_public_output_is_redacted(capsys.readouterr().out)
    assert public == {
        "status": "reviewed",
        "action": "accept",
        "candidate_status": "accepted",
    }


def test_reject_sends_only_action_and_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    result = review_cli.main(
        ["reject", "--reason", "保留现有正式决策"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=recording_opener(requests),
    )

    assert result == 0
    assert json.loads(requests[1].data) == {
        "action": "reject",
        "change_reason": "保留现有正式决策",
    }
    public = assert_public_output_is_redacted(capsys.readouterr().out)
    assert public["candidate_status"] == "rejected"


def test_list_reports_only_the_count_without_candidate_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    result = review_cli.main(
        ["list"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=recording_opener(requests),
    )

    assert result == 0
    assert [request.method for request in requests] == ["GET"]
    assert assert_public_output_is_redacted(capsys.readouterr().out) == {
        "status": "ready",
        "candidate_count": 1,
    }


@pytest.mark.parametrize("count", [0, 2])
def test_review_rejects_zero_or_multiple_proposed_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    count: int,
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []
    proposed = [candidate(f"candidate-{index}") for index in range(count)]

    result = review_cli.main(
        ["accept", "--reason", "负责人确认"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=recording_opener(requests, proposed=proposed),
    )

    assert result == 1
    assert [request.method for request in requests] == ["GET"]
    assert assert_public_output_is_redacted(capsys.readouterr().out) == {
        "status": "blocked",
        "error_type": "review_not_ready",
    }


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE],
                "can_review": True,
            },
            "owner-2": {
                "token_sha256": hashlib.sha256(b"other").hexdigest(),
                "project_ids": [PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE],
                "can_review": True,
            },
        },
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE],
                "can_review": False,
            }
        },
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID, "project-2"],
                "permission_scopes": [PERMISSION_SCOPE],
                "can_review": True,
            }
        },
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID, PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE],
                "can_review": True,
            }
        },
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE, "project:other"],
                "can_review": True,
            }
        },
        {
            "owner-1": {
                "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                "project_ids": [PROJECT_ID],
                "permission_scopes": [PERMISSION_SCOPE, PERMISSION_SCOPE],
                "can_review": True,
            }
        },
    ],
)
def test_invalid_or_ambiguous_reviewer_policy_fails_before_dpapi_or_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    policy: dict[str, object],
) -> None:
    private_root = write_review_fixture(tmp_path, policy=policy)
    requests: list[object] = []
    protector = FixtureProtector()

    result = review_cli.main(
        ["list"],
        private_root=private_root,
        protector=protector,
        opener=recording_opener(requests),
    )

    assert result == 1
    assert protector.calls == []
    assert requests == []
    assert assert_public_output_is_redacted(capsys.readouterr().out) == {
        "status": "blocked",
        "error_type": "review_not_ready",
    }


@pytest.mark.parametrize(
    ("protector", "expected_calls"),
    [
        (FailingProtector(), None),
        (FixtureProtector(plaintext=b"wrong-token"), 1),
    ],
)
def test_dpapi_failure_or_token_hash_mismatch_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    protector: object,
    expected_calls: int | None,
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    result = review_cli.main(
        ["list"],
        private_root=private_root,
        protector=protector,
        opener=recording_opener(requests),
    )

    assert result == 1
    if expected_calls is not None:
        assert len(protector.calls) == expected_calls
        assert protector.calls[0][0] == PROJECT_ID
    assert requests == []
    public = assert_public_output_is_redacted(capsys.readouterr().out)
    assert public["error_type"] == "review_not_ready"


def test_redirected_response_is_rejected_before_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    def redirecting(request, *, timeout: float):  # type: ignore[no-untyped-def]
        requests.append(request)
        return FakeResponse(
            {"conflicts": [candidate()]},
            url="http://example.invalid/stolen",
        )

    result = review_cli.main(
        ["accept", "--reason", "负责人确认"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=redirecting,
    )

    assert result == 1
    assert len(requests) == 1
    assert assert_public_output_is_redacted(capsys.readouterr().out)["status"] == (
        "blocked"
    )


def test_default_opener_disables_proxies_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    sentinel = object()

    def capture_build_opener(*handlers: object) -> object:
        captured.extend(handlers)
        return sentinel

    monkeypatch.setattr(review_cli, "build_opener", capture_build_opener)

    assert review_cli._build_direct_opener() is sentinel
    proxy_handlers = [
        handler for handler in captured if isinstance(handler, ProxyHandler)
    ]
    redirect_handlers = [
        handler
        for handler in captured
        if isinstance(handler, review_cli._RejectRedirects)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert len(redirect_handlers) == 1
    assert (
        redirect_handlers[0].redirect_request(
            None,
            None,
            302,
            "Found",
            None,
            "http://example.invalid/stolen",
        )
        is None
    )


def test_oversized_response_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = write_review_fixture(tmp_path)

    def oversized(request, *, timeout: float):  # type: ignore[no-untyped-def]
        return FakeResponse(
            b"x" * (review_cli.MAX_RESPONSE_BYTES + 1),
            url=request.full_url,
        )

    result = review_cli.main(
        ["list"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=oversized,
    )

    assert result == 1
    assert assert_public_output_is_redacted(capsys.readouterr().out)["status"] == (
        "blocked"
    )


@pytest.mark.parametrize("failure", ["stale", "status", "identifier"])
def test_review_fails_closed_when_candidate_changes_or_is_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    def failing(request, *, timeout: float):  # type: ignore[no-untyped-def]
        requests.append(request)
        if request.method == "GET":
            return FakeResponse(
                {"conflicts": [candidate()]},
                url=request.full_url,
            )
        if failure == "stale":
            raise HTTPError(request.full_url, 409, "stale", {}, None)
        returned_id = "candidate-other" if failure == "identifier" else "candidate-1"
        returned_status = "proposed" if failure == "status" else "accepted"
        return FakeResponse(
            {"candidate": candidate(returned_id, status=returned_status)},
            url=request.full_url,
        )

    result = review_cli.main(
        ["accept", "--reason", "负责人确认"],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=failing,
    )

    assert result == 1
    assert [request.method for request in requests] == ["GET", "POST"]
    assert assert_public_output_is_redacted(capsys.readouterr().out)["status"] == (
        "blocked"
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--url", "http://example.invalid"],
        ["--candidate", "candidate-1"],
        ["--project", PROJECT_ID],
        ["--reviewer", "owner-1"],
        ["--token", TOKEN],
    ],
)
def test_cli_rejects_boundary_overrides_without_echoing_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    private_root = write_review_fixture(tmp_path)
    requests: list[object] = []

    result = review_cli.main(
        ["list", *extra],
        private_root=private_root,
        protector=FixtureProtector(),
        opener=recording_opener(requests),
    )

    assert result == 2
    assert requests == []
    captured = capsys.readouterr()
    assert captured.err == ""
    assert assert_public_output_is_redacted(captured.out) == {
        "status": "blocked",
        "error_type": "invalid_command",
    }


def test_blank_reason_is_rejected_before_private_or_network_access(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_root = tmp_path / "missing"

    result = review_cli.main(
        ["accept", "--reason", "   "],
        private_root=missing_root,
        protector=FixtureProtector(),
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be used"),
    )

    assert result == 2
    assert assert_public_output_is_redacted(capsys.readouterr().out) == {
        "status": "blocked",
        "error_type": "invalid_command",
    }


@pytest.mark.parametrize(
    ("filename", "limit_name"),
    [
        ("principal.json", "MAX_POLICY_BYTES"),
        ("credential.dpapi", "MAX_CREDENTIAL_BYTES"),
    ],
)
@pytest.mark.parametrize(
    "unsafe_kind",
    ["oversized", "hardlink", "symlink", "reparse"],
)
def test_unsafe_private_files_fail_before_dpapi_and_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    limit_name: str,
    unsafe_kind: str,
) -> None:
    private_root = write_review_fixture(tmp_path)
    private_file = private_root / filename
    original = private_file.read_bytes()
    if unsafe_kind == "oversized":
        limit = getattr(review_cli, limit_name)
        private_file.write_bytes(b"x" * (limit + 1))
    elif unsafe_kind in {"hardlink", "symlink"}:
        backing = tmp_path / f"{unsafe_kind}-{filename}"
        backing.write_bytes(original)
        private_file.unlink()
        if unsafe_kind == "hardlink":
            private_file.hardlink_to(backing)
            assert private_file.lstat().st_nlink > 1
        else:
            try:
                private_file.symlink_to(backing)
            except (NotImplementedError, OSError) as exc:
                pytest.skip(f"file symlinks unavailable: {exc}")
            details = private_file.lstat()
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
            assert stat.S_ISLNK(details.st_mode) or (
                getattr(details, "st_file_attributes", 0) & reparse_flag
            )
    else:
        real_lstat = Path.lstat

        def reparse_lstat(path: Path):  # type: ignore[no-untyped-def]
            details = real_lstat(path)
            if path != private_file:
                return details
            return SimpleNamespace(
                st_mode=details.st_mode,
                st_nlink=details.st_nlink,
                st_size=details.st_size,
                st_file_attributes=getattr(
                    stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    1024,
                ),
            )

        monkeypatch.setattr(Path, "lstat", reparse_lstat)
    protector = FixtureProtector()

    result = review_cli.main(
        ["list"],
        private_root=private_root,
        protector=protector,
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be used"),
    )

    assert result == 1
    assert protector.calls == []
    assert assert_public_output_is_redacted(capsys.readouterr().out)["status"] == (
        "blocked"
    )
