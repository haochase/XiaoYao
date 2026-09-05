from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import re
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from threading import BrokenBarrierError
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import ProxyHandler

import pytest
from pydantic import ValidationError

from companion_gateway.project.models import (
    EvidenceRef,
    ProjectContextPackage,
    SourcedFact,
)
from companion_gateway.project.sync_models import SourceErrorType
import tools.dws_project_sync as sync_cli
import tools.dws_sync.state_lock as state_lock
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


def sourced_fact(
    *,
    source_type: str = "document",
    source_id: str = "doc-1",
    excerpt: str = "采用 方案 B。",
) -> SourcedFact:
    record = active_record(source_type=source_type, source_id=source_id)
    return SourcedFact(
        text="采用方案 B",
        source_refs=(
            EvidenceRef(
                source_type=record.source_type.value,
                source_id=record.source_id,
                source_title=record.source_title or "",
                source_url=record.source_url or "",
                source_time=record.source_time or NOW,
                excerpt=excerpt,
                permission_scope=record.permission_scope,
            ),
        ),
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
        completed_retrieval_request_ids=(),
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


def pending_args(paths: dict[str, Path]) -> list[str]:
    return [
        "pending",
        "--manifest",
        str(paths["manifest"]),
        "--project",
        "project-1",
        "--sources-file",
        str(paths["sources"]),
        "--gateway",
        "http://127.0.0.1:8731",
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
        self.read_sizes: list[int | None] = []

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

        def read(size: int | None = None) -> bytes:
            self.read_sizes.append(size)
            return canonical(response).encode("utf-8")

        return SimpleNamespace(
            read=read,
            __enter__=lambda self: self,
            __exit__=lambda *_args: None,
        )


def _concurrent_push_worker(
    args: list[str],
    start_barrier: object,
    load_barrier: object,
    observations: object,
) -> None:
    original_load_state = sync_cli._load_state

    def synchronized_load_state(
        path: Path, project_id: str
    ):  # type: ignore[no-untyped-def]
        state = original_load_state(path, project_id)
        observations.put(  # type: ignore[attr-defined]
            ("loaded", state.last_cursor, state.pending is None)
        )
        try:
            load_barrier.wait(timeout=5)  # type: ignore[attr-defined]
        except BrokenBarrierError:
            pass
        return state

    def urlopen(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 30.0
        request_payload = json.loads(request.data)
        observations.put(  # type: ignore[attr-defined]
            ("sent", request_payload["source_cursor"], request_payload["sync_id"])
        )
        response = {
            "sync_id": request_payload["sync_id"],
            "outcome": "applied",
            "project_status": "healthy",
            "accepted_sources": 1,
            "failed_sources": 0,
            "generation_id": "generation-private",
            "next_sync_before": "2026-09-05T12:05:00+00:00",
        }
        return SimpleNamespace(
            read=lambda _size: canonical(response).encode("utf-8"),
            close=lambda: None,
        )

    start_barrier.wait(timeout=5)  # type: ignore[attr-defined]
    sync_cli._load_state = synchronized_load_state
    result = main(
        args,
        urlopen=urlopen,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    )
    observations.put(("result", result))  # type: ignore[attr-defined]


def _blocking_push_worker(
    args: list[str],
    entered_http: object,
    release_http: object,
    observations: object,
) -> None:
    def urlopen(request, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 30.0
        request_payload = json.loads(request.data)
        observations.put(  # type: ignore[attr-defined]
            ("sent", request_payload["source_cursor"], request_payload["sync_id"])
        )
        entered_http.set()  # type: ignore[attr-defined]
        if not release_http.wait(timeout=5):  # type: ignore[attr-defined]
            raise TimeoutError
        response = {
            "sync_id": request_payload["sync_id"],
            "outcome": "applied",
            "project_status": "healthy",
            "accepted_sources": 1,
            "failed_sources": 0,
            "generation_id": "generation-private",
            "next_sync_before": "2026-09-05T12:05:00+00:00",
        }
        return SimpleNamespace(
            read=lambda _size: canonical(response).encode("utf-8"),
            close=lambda: None,
        )

    result = main(
        args,
        urlopen=urlopen,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    )
    observations.put(("result", result))  # type: ignore[attr-defined]


class PendingUrlOpen:
    def __init__(self, source_hash: str) -> None:
        self.source_hash = source_hash
        self.request = None

    def __call__(self, request, *, timeout: float):  # type: ignore[no-untyped-def]
        self.request = request
        assert timeout == 30.0
        payload = {
            "requests": [
                {
                    "request_id": "retrieval-1",
                    "project_id": "project-1",
                    "query_hash": digest("missing detail"),
                    "source_id_hashes": [self.source_hash],
                    "baseline_generation_id": "generation-1",
                    "baseline_content_hash": "a" * 64,
                    "baseline_source_cursor": 1,
                    "baseline_sources": [
                        {
                            "source_id_hash": self.source_hash,
                            "source_version": "v1",
                            "content_hash": "b" * 64,
                            "chunk_fingerprint": "c" * 64,
                        }
                    ],
                    "status": "in_progress",
                    "created_at": NOW.isoformat(),
                    "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
                    "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                    "attempt_count": 1,
                    "completed_at": None,
                }
            ]
        }
        return SimpleNamespace(
            read=lambda size: canonical(payload).encode("utf-8"),
            close=lambda: None,
        )


def test_pending_fetch_maps_gateway_hashes_into_private_source_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = PendingUrlOpen(digest("doc-1"))

    assert main(
        pending_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0

    output = json.loads(capsys.readouterr().out)
    private = json.loads(paths["sources"].read_text(encoding="utf-8"))
    request = private["retrieval_requests"][0]
    assert sent.request.full_url.endswith(
        "/v1/projects/project-1/retrieval-requests?status=pending"
    )
    assert sent.request.method == "GET"
    assert sent.request.get_header("Authorization") == "Bearer private-token"
    assert request == {
        "request_id": "retrieval-1",
        "query_hash": digest("missing detail"),
        "sources": [{"source_id": "doc-1", "source_type": "document"}],
    }
    assert output == {
        "status": "pending_fetched",
        "project_id": "project-1",
        "request_count": 1,
        "source_count": 1,
        "content_hash": private["content_hash"],
    }
    assert "private-token" not in canonical(output)


def test_pending_fetch_rejects_unmapped_source_without_replacing_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    original = paths["sources"].read_bytes()

    assert main(
        pending_args(paths),
        urlopen=PendingUrlOpen("f" * 64),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "retrieval_request_invalid",
    }
    assert paths["sources"].read_bytes() == original


def test_push_completes_only_requests_present_in_source_bundle(
    tmp_path: Path,
    capsys,
) -> None:
    paths = write_push_inputs(tmp_path)
    assert main(
        pending_args(paths),
        urlopen=PendingUrlOpen(digest("doc-1")),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    claimed = QwenProjectContextArtifact(
        schema_version=1,
        context=context(),
        completed_retrieval_request_ids=("retrieval-1",),
    )
    write_json(paths["context"], claimed.model_dump(mode="json"))

    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("dry-run must not use network"),
        environ={},
    ) == 0
    capsys.readouterr()

    unclaimed = claimed.model_copy(
        update={"completed_retrieval_request_ids": ("retrieval-other",)}
    )
    write_json(paths["context"], unclaimed.model_dump(mode="json"))
    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("invalid request must not use network"),
        environ={},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "retrieval_request_invalid",
    }


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


def test_collect_rejects_oversized_bundle_before_atomic_write(
    tmp_path: Path,
    capsys,
) -> None:
    selected = project()
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "private-bundle.json"
    output.write_bytes(b"existing-private-state")
    dws_path = tmp_path / "dws.exe"
    dws_path.write_bytes(b"")
    write_manifest(manifest, selected)

    class OversizedDws(FakeDws):
        def run(self, args: tuple[str, ...]) -> dict[str, object]:
            if args[:2] == ("doc", "read"):
                return {"markdown": "采用方案 B。" + "x" * 1_100_000}
            return super().run(args)

    assert main(
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
        runner=OversizedDws(),
        urlopen=lambda *_a, **_k: pytest.fail("collect must not use network"),
        now=lambda: NOW,
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "sources_file_too_large",
    }
    assert output.read_bytes() == b"existing-private-state"


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


@pytest.mark.parametrize(
    "field",
    ["sourced_actions", "sourced_risks", "sourced_next_meeting"],
)
def test_build_envelope_validates_each_sourced_fact_field(field: str) -> None:
    fact = sourced_fact()
    value: object = fact if field == "sourced_next_meeting" else (fact,)
    sourced_context = context().model_copy(update={field: value})

    envelope = build_envelope(project(), bundle(), sourced_context, now=NOW)

    assert getattr(envelope.context, field) == value


@pytest.mark.parametrize(
    "field",
    ["sourced_actions", "sourced_risks", "sourced_next_meeting"],
)
@pytest.mark.parametrize(
    ("records", "fact", "error_type"),
    [
        (
            (
                active_record(),
                DwsSourceRecord(
                    source_type="task",
                    source_id="task-1",
                    permission_scope=SCOPE,
                    fetched_at=NOW,
                    status="failed",
                    error_type=SourceErrorType.NETWORK_TIMEOUT,
                    retryable=True,
                ),
            ),
            sourced_fact(source_type="task", source_id="task-1"),
            "source_ref_mismatch",
        ),
        (
            (active_record(),),
            sourced_fact(excerpt="来源中不存在的摘录"),
            "source_excerpt_mismatch",
        ),
    ],
)
def test_build_envelope_rejects_invalid_sourced_fact_references(
    field: str,
    records: tuple[DwsSourceRecord, ...],
    fact: SourcedFact,
    error_type: str,
) -> None:
    selected = project(
        sources=tuple(
            source_spec(record.source_type.value, record.source_id)
            for record in records
        )
    )
    value: object = fact if field == "sourced_next_meeting" else (fact,)
    sourced_context = context().model_copy(update={field: value})

    with pytest.raises(ValueError, match=error_type):
        build_envelope(selected, bundle(*records), sourced_context, now=NOW)


def test_qwen_artifact_and_push_reject_nonempty_legacy_facts(
    tmp_path: Path,
    capsys,
) -> None:
    legacy_context = context().model_copy(
        update={"open_actions": ("无来源行动项",)}
    )
    with pytest.raises(ValidationError, match="context_fact_unreferenced"):
        QwenProjectContextArtifact(
            schema_version=1,
            context=legacy_context,
        )

    paths = write_push_inputs(tmp_path)
    write_json(
        paths["context"],
        {
            "schema_version": 1,
            "context": legacy_context.model_dump(mode="json"),
            "completed_retrieval_request_ids": [],
        },
    )

    assert main(
        push_args(paths, "--dry-run"),
        urlopen=lambda *_a, **_k: pytest.fail("invalid facts must fail first"),
        environ={},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "context_fact_unreferenced",
    }
    assert not paths["state"].exists()


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

    lock_files = list(paths["state"].parent.glob(".dws-sync-state-*.lock"))
    assert len(lock_files) == 1
    assert lock_files[0].parent == paths["state"].parent
    assert "project-1" not in lock_files[0].name


def test_concurrent_pushes_serialize_state_lifecycle_across_processes(
    tmp_path: Path,
) -> None:
    paths = write_push_inputs(tmp_path)
    process_context = multiprocessing.get_context("spawn")
    start_barrier = process_context.Barrier(3)
    load_barrier = process_context.Barrier(2)
    observations = process_context.Queue()
    processes = [
        process_context.Process(
            target=_concurrent_push_worker,
            args=(push_args(paths), start_barrier, load_barrier, observations),
        )
        for _ in range(2)
    ]

    try:
        for process in processes:
            process.start()
        start_barrier.wait(timeout=5)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    observed = [observations.get(timeout=2) for _ in range(6)]
    loaded = sorted(item[1:] for item in observed if item[0] == "loaded")
    sent = sorted(item[1:] for item in observed if item[0] == "sent")
    results = [item[1] for item in observed if item[0] == "result"]
    state = json.loads(paths["state"].read_text(encoding="utf-8"))

    assert loaded == [(0, True), (1, True)]
    assert [item[0] for item in sent] == [1, 2]
    assert len({item[1] for item in sent}) == 2
    assert results == [0, 0]
    assert state["last_cursor"] == 2
    assert state["pending"] is None


def test_push_lock_is_held_through_http_and_timeout_is_public(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    process_context = multiprocessing.get_context("spawn")
    entered_http = process_context.Event()
    release_http = process_context.Event()
    observations = process_context.Queue()
    holder = process_context.Process(
        target=_blocking_push_worker,
        args=(push_args(paths), entered_http, release_http, observations),
    )
    contender_sent = False

    def contender_urlopen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal contender_sent
        contender_sent = True
        return RecordingUrlOpen()(*_args, **_kwargs)

    try:
        holder.start()
        assert entered_http.wait(timeout=5)
        monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)

        assert main(
            push_args(paths),
            urlopen=contender_urlopen,
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "error",
            "error_type": "sync_lock_timeout",
        }
        assert contender_sent is False
    finally:
        release_http.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    holder_observations = [observations.get(timeout=2) for _ in range(2)]
    assert [item[0] for item in holder_observations] == ["sent", "result"]


def test_state_lock_wait_is_capped_at_thirty_seconds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = iter((0.0, 30.0))
    sleeps: list[float] = []

    def unavailable(_stream: object) -> None:
        raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(state_lock, "_try_lock", unavailable)
    with pytest.raises(ValueError, match="^sync_lock_timeout$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            timeout=300.0,
            monotonic=lambda: next(clock),
            sleep=sleeps.append,
        ):
            pytest.fail("unavailable lock must not be yielded")

    assert sleeps == []


def test_state_lock_does_not_retry_non_contention_os_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def io_failure(_stream: object) -> None:
        raise OSError(errno.EIO, "private detail")

    monkeypatch.setattr(state_lock, "_try_lock", io_failure)
    with pytest.raises(ValueError, match="^private_file_write_failed$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            sleep=lambda _delay: pytest.fail("I/O errors must not be retried"),
        ):
            pytest.fail("failed lock must not be yielded")


def test_state_lock_does_not_retry_after_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempts = 0
    clock = iter((0.0, 29.99, 30.01))

    def available_too_late(_stream: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(errno.EACCES, "locked")

    monkeypatch.setattr(state_lock, "_try_lock", available_too_late)
    with pytest.raises(ValueError, match="^sync_lock_timeout$"):
        with state_lock.acquire_state_lock(
            tmp_path / "state.json",
            "project-1",
            monotonic=lambda: next(clock),
            sleep=lambda _delay: None,
        ):
            pytest.fail("lock acquired after deadline must not be yielded")

    assert attempts == 1


def test_state_lock_identity_depends_only_on_resolved_state_path(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"

    assert state_lock._state_lock_path(
        state_path, "project-1"
    ) == state_lock._state_lock_path(state_path, "project-2")
    assert state_lock._state_lock_path(
        state_path, "project-1"
    ) != state_lock._state_lock_path(tmp_path / "other-state.json", "project-1")


def test_different_state_files_do_not_block_each_other(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    independent_paths = {**paths, "state": tmp_path / "independent-state.json"}
    process_context = multiprocessing.get_context("spawn")
    entered_http = process_context.Event()
    release_http = process_context.Event()
    observations = process_context.Queue()
    holder = process_context.Process(
        target=_blocking_push_worker,
        args=(push_args(paths), entered_http, release_http, observations),
    )

    try:
        holder.start()
        assert entered_http.wait(timeout=5)
        monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)

        assert main(
            push_args(independent_paths),
            urlopen=RecordingUrlOpen(),
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "synced"
    finally:
        release_http.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2)

    assert holder.exitcode == 0
    assert json.loads(independent_paths["state"].read_text(encoding="utf-8"))[
        "last_cursor"
    ] == 1


def test_keyboard_interrupt_releases_push_lock(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)

    def interrupt(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    assert main(
        push_args(paths),
        urlopen=interrupt,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "interrupted",
    }

    monkeypatch.setattr(sync_cli, "SYNC_LOCK_TIMEOUT_SECONDS", 0.1)
    assert main(
        push_args(paths),
        urlopen=RecordingUrlOpen(),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "synced"


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
    huge = "# 大文档\n采用方案 B。\n" + "x" * 2_080_000
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


@pytest.mark.parametrize(
    ("file_key", "limit", "error_type", "dry_run"),
    [
        ("sources", 2_097_152, "sources_file_too_large", True),
        ("context", 2_097_152, "context_file_too_large", True),
        ("state", 65_536, "state_file_too_large", False),
    ],
)
def test_private_input_limits_fail_before_network_or_state_write(
    tmp_path: Path,
    capsys,
    file_key: str,
    limit: int,
    error_type: str,
    dry_run: bool,
) -> None:
    paths = write_push_inputs(tmp_path)
    oversized = b"x" * (limit + 1)
    paths[file_key].write_bytes(oversized)
    extra = ("--dry-run",) if dry_run else ()

    assert main(
        push_args(paths, *extra),
        urlopen=lambda *_a, **_k: pytest.fail("size gate must precede network"),
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": error_type,
    }
    if file_key == "state":
        assert paths["state"].read_bytes() == oversized
    else:
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


def test_default_transport_disables_environment_and_system_proxies(
    monkeypatch,
) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr(
        "urllib.request.getproxies",
        lambda: pytest.fail("system proxies must not be loaded"),
    )
    captured_handlers: list[object] = []
    real_build_opener = sync_cli.build_opener

    def recording_build_opener(*handlers: object):
        captured_handlers.extend(handlers)
        return real_build_opener(*handlers)

    monkeypatch.setattr(sync_cli, "build_opener", recording_build_opener)

    sync_cli._build_direct_opener()

    proxy_handlers = [
        handler for handler in captured_handlers if isinstance(handler, ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_default_transport_rejects_redirect_without_forwarding_bearer(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    paths = write_push_inputs(tmp_path)
    target_authorizations: list[str | None] = []
    redirect_requests = 0

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target_authorizations.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal redirect_requests
            redirect_requests += 1
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/outside",
            )
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setattr(
            sync_cli,
            "_gateway_base",
            lambda _value: f"http://127.0.0.1:{redirect.server_port}",
        )
        assert main(
            push_args(paths),
            environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
        ) == 1
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert redirect_requests == 1
    assert target_authorizations == []
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "http_error",
    }
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


@pytest.mark.parametrize(
    "field,value",
    [("outcome", []), ("project_status", {"private": "value"})],
)
def test_response_enum_fields_require_strings(
    tmp_path: Path,
    capsys,
    field: str,
    value: object,
) -> None:
    paths = write_push_inputs(tmp_path)
    response = {
        "sync_id": "placeholder",
        "outcome": "applied",
        "project_status": "healthy",
        "accepted_sources": 1,
        "failed_sources": 0,
        "generation_id": None,
        "next_sync_before": "2026-09-05T12:05:00+00:00",
    }

    class InvalidEnumResponse(RecordingUrlOpen):
        def __call__(self, request, *, timeout):  # type: ignore[no-untyped-def]
            request_payload = json.loads(request.data)
            response["sync_id"] = request_payload["sync_id"]
            response[field] = value
            return super().__call__(request, timeout=timeout)

    sent = InvalidEnumResponse(response)
    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "response_invalid",
    }
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


def test_response_read_is_bounded_to_sixty_four_kibibytes(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    sent = RecordingUrlOpen()

    assert main(
        push_args(paths),
        urlopen=sent,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 0
    capsys.readouterr()
    assert sent.read_sizes == [65_537]


def test_oversized_response_is_rejected_after_one_bounded_read(
    tmp_path: Path, capsys
) -> None:
    paths = write_push_inputs(tmp_path)
    read_sizes: list[int] = []

    def oversized(_request, *, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 30.0

        def read(size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

        return SimpleNamespace(read=read)

    assert main(
        push_args(paths),
        urlopen=oversized,
        environ={"COMPANION_DWS_SYNC_TOKEN": "private-token"},
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "response_invalid",
    }
    assert read_sizes == [65_537]
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["pending"]


@pytest.mark.parametrize("argv", [["--help"], ["collect", "--help"]])
def test_help_outputs_exactly_one_json_object(
    capsys, argv: list[str]
) -> None:
    assert main(argv) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == "help"
    assert output.err == ""
    assert output.out.count("\n") == 1


def test_qwen_prompt_only_completes_retrieval_with_obtained_evidence() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    normalized = " ".join(prompt.replace("`", "").split())
    assert "未完成的检索请求绝不能加入 completed_retrieval_request_ids" in normalized
    assert "只有已取得对应证据" in normalized
    assert "遗漏的请求保持 pending" in normalized
    assert "python -m tools.dws_project_sync pending" in normalized
    assert "retrieval_requests" in normalized
    assert "request_id" in normalized
    assert "query_hash" in normalized
    assert "sources" in normalized
    collect_at = normalized.index("python -m tools.dws_project_sync collect")
    pending_at = normalized.index("python -m tools.dws_project_sync pending")
    skill_at = normalized.index("hui-anchor-dws-project-context-v1", pending_at)
    push_at = normalized.index("python -m tools.dws_project_sync push")
    assert collect_at < pending_at < skill_at < push_at


def test_qwen_prompt_uses_only_module_cli_entrypoints() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "python -m tools.dws_project_sync collect" in prompt
    assert "python -m tools.dws_project_sync push" in prompt
    assert "tools/dws_project_sync.py collect" not in prompt
    assert "tools/dws_project_sync.py push" not in prompt


def test_qwen_prompt_defines_fixed_strict_private_task_config() -> None:
    root = Path(__file__).resolve().parents[2]
    prompt = (root / "prompts" / "qwenwork-dws-project-sync.md").read_text(
        encoding="utf-8"
    )
    schema_match = re.search(
        r"<!-- task-config-schema -->\s*```json\s*(\{.*?\})\s*```",
        prompt,
        re.DOTALL,
    )

    assert schema_match is not None
    schema = json.loads(schema_match.group(1))
    fields = {
        "schema_version",
        "manifest",
        "project",
        "dws",
        "source_bundle",
        "context_artifact",
        "state",
    }
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == fields
    assert set(schema["properties"]) == fields
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["properties"]["project"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    assert ".private/qwenwork-dws-project-sync.json" in prompt
    forbidden_placeholder = "<" + "PRIVATE_"
    assert forbidden_placeholder not in prompt
    lstat_at = prompt.index("Path.lstat")
    read_at = prompt.index("read(65537)")
    parse_at = prompt.index("JSON 解析")
    assert "普通文件" in prompt[lstat_at:read_at]
    assert "symlink" in prompt[lstat_at:read_at]
    assert "reparse" in prompt[lstat_at:read_at]
    assert lstat_at < read_at < parse_at


def test_qwen_prompt_rejects_private_path_aliases() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "Path.resolve(strict=False)" in prompt
    assert "os.path.normcase" in prompt
    assert "os.path.samefile" in prompt
    assert "五个配置路径与固定任务配置路径必须两两不同" in prompt
    assert "reparse" in prompt


def test_qwen_prompt_names_skill_and_closes_artifact_io_contract() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")

    assert "hui-anchor-dws-project-context-v1" in prompt
    assert "DwsSourceBundle" in prompt
    assert "QwenProjectContextArtifact" in prompt
    assert "唯一输入" in prompt
    assert "唯一输出" in prompt
    assert "同目录临时文件" in prompt
    assert "flush" in prompt
    assert "fsync" in prompt
    assert "os.replace" in prompt
    assert "不得读取其他文件" in prompt
    assert "不得输出其他内容" in prompt
    assert '"open_actions": []' in prompt
    assert '"current_risks": []' in prompt
    assert '"next_meeting": null' in prompt
    assert "必须预先安装" in prompt
    assert "不可用时立即停止" in prompt
    assert "不得搜索、安装或替换 Skill" in prompt

    validate_at = prompt.index("QwenProjectContextArtifact.model_validate")
    size_at = prompt.index("2097152")
    temporary_at = prompt.index("创建同目录临时文件")
    replace_at = prompt.index("os.replace")
    assert validate_at < temporary_at
    assert size_at < temporary_at
    assert temporary_at < replace_at


def test_private_task_config_is_ignored_and_documented_publicly() -> None:
    root = Path(__file__).resolve().parents[2]
    ignore_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    readme = (root / "README.md").read_text(encoding="utf-8")
    gateway_readme = (root / "gateway" / "README.md").read_text(
        encoding="utf-8"
    )

    assert ".private/" in ignore_lines
    for document in (readme, gateway_readme):
        assert ".private/qwenwork-dws-project-sync.json" in document
        assert "hui-anchor-dws-project-context-v1" in document
