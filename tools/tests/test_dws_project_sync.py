from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from companion_gateway.project.models import ProjectContextPackage
from companion_gateway.project.sync_models import SourceErrorType
from tools.dws_project_sync import (
    QwenProjectContextArtifact,
    build_envelope,
    main,
)
from tools.dws_sync import (
    DwsProjectManifest,
    DwsSourceBundle,
    DwsSourceRecord,
    DwsSourceSpec,
)


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
SCOPE = "project:project-1"


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
        completed_retrieval_request_ids=("retrieval-1",),
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


class FakeDws:
    def run(self, args: tuple[str, ...]) -> dict[str, object]:
        if args[:2] == ("doc", "info"):
            return {
                "nodeId": "doc-1",
                "contentType": "ALIDOC",
                "extension": "adoc",
                "title": "决策文档",
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
        return SimpleNamespace(
            read=lambda: canonical(response).encode("utf-8"),
            __enter__=lambda self: self,
            __exit__=lambda *_args: None,
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
    assert envelope.generated_at == context().generated_at
    assert [item.status.value for item in envelope.sources] == ["active", "failed"]
    assert len(envelope.sources[0].chunks) == 1
    source_id_hash = digest("task-private")
    assert envelope.sources[1].source_title == f"task:{source_id_hash[:12]}"
    assert envelope.sources[1].source_url == f"dingtalk://task/{source_id_hash}"
    assert [item.status.value for item in envelope.tombstones] == ["deleted"]


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
        "last_content_hash": request_payload["content_hash"],
        "last_cursor": 1,
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
    pending = json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]
    assert first_output == {"status": "error", "error_type": "network_error"}

    sent = RecordingUrlOpen()
    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    assert json.loads(sent.request.data)["sync_id"] == pending["sync_id"]


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
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


def test_payload_over_limit_fails_before_state_or_network(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    huge = "# 大文档\n采用方案 B。\n" + "甲" * 2_100_000
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
