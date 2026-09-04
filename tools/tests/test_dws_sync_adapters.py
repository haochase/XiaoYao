from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from companion_gateway.project.sync_models import SourceErrorType, SyncSourceType
from tools.dws_sync.adapters import (
    DwsSourceBundle,
    DwsSourceRecord,
    collect_sources,
    read_calendar_event,
    read_document,
    read_meeting_note,
    read_task,
    unwrap_dws_payload,
)
from tools.dws_sync.manifest import DwsProjectManifest, DwsSourceSpec
from tools.dws_sync.runner import DwsCommandRunner, DwsReadError


NOW = datetime(2026, 9, 5, 4, tzinfo=UTC)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source(source_type: str, source_id: str, **extra: object) -> DwsSourceSpec:
    return DwsSourceSpec(
        source_type=source_type,
        source_id=source_id,
        **extra,
    )


class RecordingRunner:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...]) -> dict[str, object]:
        self.calls.append(args)
        return self.responses.pop(0)


def test_runner_injects_fixed_profile_json_format_and_safe_subprocess(
    tmp_path: Path,
) -> None:
    dws_path = tmp_path / "dws.exe"
    dws_path.write_text("", encoding="utf-8")
    calls: list[tuple[object, ...]] = []

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((*args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    runner = DwsCommandRunner(dws_path, profile="corp:user", run=fake_run)

    assert runner.run(("todo", "task", "get", "--task-id", "task-1")) == {
        "ok": True
    }
    assert calls == [
        (
            [
                str(dws_path),
                "--profile",
                "corp:user",
                "todo",
                "task",
                "get",
                "--task-id",
                "task-1",
                "--format",
                "json",
            ],
            {
                "shell": False,
                "capture_output": True,
                "text": True,
                "timeout": 30.0,
            },
        )
    ]


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--format", "json"),
        ("--profile", "other"),
        ("--token", "secret"),
        ("--client-id", "secret"),
        ("--client-secret", "secret"),
        ("--token=secret",),
        ("--format=json",),
        ("--yes",),
        ("|",),
        ("&&",),
    ],
)
def test_runner_rejects_caller_controlled_global_or_shell_args(
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    dws_path = tmp_path / "dws.exe"
    dws_path.write_text("", encoding="utf-8")
    runner = DwsCommandRunner(
        dws_path,
        profile="corp:user",
        run=lambda *_a, **_k: None,
    )

    with pytest.raises(ValueError, match="dws_args_invalid"):
        runner.run(args)


def test_runner_normalizes_timeout_and_failures_without_leaking_output(
    tmp_path: Path,
) -> None:
    dws_path = tmp_path / "dws.exe"
    dws_path.write_text("", encoding="utf-8")

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("private command", 30, stderr="secret")

    with pytest.raises(DwsReadError) as timeout_error:
        DwsCommandRunner(dws_path, profile="corp:user", run=timeout).run(
            ("doc", "info")
        )
    assert timeout_error.value.error_type is SourceErrorType.NETWORK_TIMEOUT
    assert timeout_error.value.retryable is True
    assert str(timeout_error.value) == "network_timeout"
    assert timeout_error.value.__cause__ is None
    assert timeout_error.value.__context__ is None

    def failed(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout=canonical(
                {
                    "success": False,
                    "error": {
                        "error_type": "permission_denied",
                        "retryable": False,
                        "message": "private stdout",
                    },
                }
            ),
            stderr="private stderr",
        )

    with pytest.raises(DwsReadError) as failure:
        DwsCommandRunner(dws_path, profile="corp:user", run=failed).run(("doc", "info"))
    assert failure.value.error_type is SourceErrorType.PERMISSION_DENIED
    assert failure.value.retryable is False
    assert str(failure.value) == "permission_denied"
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("stdout", ["[]", "null", "not-json"])
def test_runner_exit_zero_requires_one_json_object(
    tmp_path: Path,
    stdout: str,
) -> None:
    dws_path = tmp_path / "dws.exe"
    dws_path.write_text("", encoding="utf-8")
    run = lambda *_a, **_k: SimpleNamespace(  # noqa: E731
        returncode=0,
        stdout=stdout,
        stderr="private",
    )

    with pytest.raises(DwsReadError) as error:
        DwsCommandRunner(dws_path, profile="corp:user", run=run).run(("doc", "info"))
    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_runner_rejects_non_finite_json_constants(
    tmp_path: Path,
    constant: str,
) -> None:
    dws_path = tmp_path / "dws.exe"
    dws_path.write_text("", encoding="utf-8")
    run = lambda *_a, **_k: SimpleNamespace(  # noqa: E731
        returncode=0,
        stdout=f'{{"value":{constant}}}',
        stderr="private",
    )

    with pytest.raises(DwsReadError) as error:
        DwsCommandRunner(dws_path, profile="corp:user", run=run).run(("doc", "info"))

    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_unwrap_dws_payload_has_a_fixed_three_layer_allowlist() -> None:
    payload = {"nodeId": "doc-1"}
    assert unwrap_dws_payload(
        {"success": True, "body": {"data": {"result": payload}}}
    ) == payload

    invalid = [
        {"body": {}, "data": {}},
        {"body": {}, "private": "secret"},
        {"body": {"data": {"result": {"body": payload}}}},
        {"body": "text"},
        {"success": False, "message": "private"},
    ]
    for response in invalid:
        with pytest.raises(DwsReadError) as error:
            unwrap_dws_payload(response)
        assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
        assert "private" not in str(error.value)


def test_unwrap_maps_structured_error_without_exposing_message() -> None:
    with pytest.raises(DwsReadError) as error:
        unwrap_dws_payload(
            {
                "success": False,
                "code": "B_PERMISSION_NoPermission",
                "message": "private message",
            }
        )

    assert error.value.error_type is SourceErrorType.PERMISSION_DENIED
    assert str(error.value) == "permission_denied"

    with pytest.raises(DwsReadError) as nested_error:
        unwrap_dws_payload(
            {
                "body": {
                    "success": False,
                    "code": "authentication_failed",
                    "result": {"private": "content"},
                }
            }
        )
    assert nested_error.value.error_type is SourceErrorType.AUTHENTICATION_FAILED


def test_document_adapter_uses_fixed_arguments_and_validates_adoc() -> None:
    info = {
        "nodeId": "doc-001",
        "contentType": "ALIDOC",
        "extension": "adoc",
        "title": "设计说明",
        "updatedAt": "2026-09-05T12:00:00+08:00",
    }
    read = {"markdown": "# 设计说明\n正文"}
    runner = RecordingRunner([{"result": info}, {"data": read}])

    record = read_document(
        runner,
        source("document", "doc-001"),
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    assert runner.calls == [
        ("doc", "info", "--node", "doc-001"),
        ("doc", "read", "--node", "doc-001"),
    ]
    assert record.status == "active"
    assert record.content_text == "# 设计说明\n正文"
    assert record.attributes_json == canonical({"info": info, "read": read})
    assert record.content_hash == hashlib.sha256(
        record.content_text.encode("utf-8")
    ).hexdigest()

    invalid = RecordingRunner(
        [{"contentType": "DOCUMENT", "extension": "pdf"}]
    )
    with pytest.raises(DwsReadError) as error:
        read_document(
            invalid,
            source("document", "doc-001"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )
    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert invalid.calls == [("doc", "info", "--node", "doc-001")]


def test_meeting_adapter_reads_all_parts_and_pages_until_token_is_empty() -> None:
    info = {"taskUuid": "abc123", "title": "评审会"}
    summary = {"markdown": "结论"}
    page_one = {"paragraphs": [{"text": "甲"}], "nextToken": "token-2"}
    page_two = {"paragraphs": [{"text": "乙"}], "nextToken": ""}
    todos = {"todos": [{"subject": "跟进"}]}
    runner = RecordingRunner([info, summary, page_one, page_two, todos])

    record = read_meeting_note(
        runner,
        source("meeting_note", "abc123"),
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    assert runner.calls == [
        ("minutes", "get", "info", "--id", "abc123"),
        ("minutes", "get", "summary", "--id", "abc123"),
        ("minutes", "get", "transcription", "--id", "abc123"),
        (
            "minutes",
            "get",
            "transcription",
            "--id",
            "abc123",
            "--next-token",
            "token-2",
        ),
        ("minutes", "get", "todos", "--id", "abc123"),
    ]
    assert json.loads(record.content_text or "") == {
        "info": info,
        "summary": summary,
        "transcription": [{"text": "甲"}, {"text": "乙"}],
        "todos": todos,
    }


def test_meeting_adapter_rejects_empty_token_page_and_repeated_token() -> None:
    prefix = [{"taskUuid": "abc123"}, {"markdown": "summary"}]
    empty = RecordingRunner(prefix + [{"items": [], "nextToken": "again"}])
    with pytest.raises(DwsReadError) as empty_error:
        read_meeting_note(
            empty,
            source("meeting_note", "abc123"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )
    assert empty_error.value.error_type is SourceErrorType.INVALID_PAYLOAD

    repeated = RecordingRunner(
        prefix
        + [
            {"records": [{"text": "one"}], "nextToken": "again"},
            {"records": [{"text": "two"}], "nextToken": "again"},
        ]
    )
    with pytest.raises(DwsReadError) as repeated_error:
        read_meeting_note(
            repeated,
            source("meeting_note", "abc123"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )
    assert repeated_error.value.error_type is SourceErrorType.INVALID_PAYLOAD


def test_meeting_adapter_stops_at_one_hundred_pages() -> None:
    responses: list[dict[str, object]] = [
        {"taskUuid": "abc123"},
        {"markdown": "summary"},
    ]
    responses.extend(
        {"paragraphs": [{"text": str(index)}], "nextToken": f"token-{index + 1}"}
        for index in range(100)
    )
    runner = RecordingRunner(responses)

    with pytest.raises(DwsReadError) as error:
        read_meeting_note(
            runner,
            source("meeting_note", "abc123"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )

    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert len(runner.calls) == 102


def test_task_adapter_only_gets_the_allowlisted_task() -> None:
    detail = {"result": {"taskId": "task-1", "subject": "交付", "done": False}}
    runner = RecordingRunner([detail])

    record = read_task(
        runner,
        source("task", "task-1"),
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    assert runner.calls == [("todo", "task", "get", "--task-id", "task-1")]
    assert record.content_text == canonical(detail["result"])


def test_adapter_rejects_non_finite_json_without_exception_chain() -> None:
    runner = RecordingRunner([{"taskId": "task-1", "value": float("nan")}])

    with pytest.raises(DwsReadError) as error:
        read_task(
            runner,
            source("task", "task-1"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )

    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_adapter_rejects_unpaired_surrogate_without_exception_chain() -> None:
    runner = RecordingRunner([{"taskId": "task-1", "value": "\ud800"}])

    with pytest.raises(DwsReadError) as error:
        read_task(
            runner,
            source("task", "task-1"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )

    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert error.value.retryable is False
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_collect_sources_isolates_unpaired_surrogate_as_failed_record() -> None:
    project = DwsProjectManifest(
        project_id="project-1",
        project_name="Demo",
        profile="private-profile",
        permission_scope="project:project-1",
        sources=(source("task", "task-1"),),
    )
    runner = RecordingRunner([{"taskId": "task-1", "value": "\ud800"}])

    bundle = collect_sources(project, runner, clock=lambda: NOW)

    assert bundle.records[0].status == "failed"
    assert bundle.records[0].error_type is SourceErrorType.INVALID_PAYLOAD
    assert bundle.records[0].retryable is False


def test_adapter_normalizes_metadata_validation_errors_and_collects_failure() -> None:
    invalid_time = RecordingRunner(
        [{"taskId": "task-1", "updatedAt": "private-invalid-time"}]
    )
    with pytest.raises(DwsReadError) as time_error:
        read_task(
            invalid_time,
            source("task", "task-1"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )
    assert time_error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert time_error.value.__cause__ is None
    assert time_error.value.__context__ is None

    project = DwsProjectManifest(
        project_id="project-1",
        project_name="Demo",
        profile="private-profile",
        permission_scope="project:project-1",
        sources=(source("task", "task-1"),),
    )
    long_metadata = RecordingRunner(
        [{"taskId": "task-1", "title": "密" * 513}]
    )

    bundle = collect_sources(project, long_metadata, clock=lambda: NOW)

    assert bundle.records[0].status == "failed"
    assert bundle.records[0].error_type is SourceErrorType.INVALID_PAYLOAD


def test_long_source_id_uses_hashed_fallback_url() -> None:
    source_id = "文" * 256
    runner = RecordingRunner([{}])

    record = read_task(
        runner,
        source("task", source_id),
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    source_hash = hashlib.sha256(source_id.encode()).hexdigest()
    assert record.source_url == f"dingtalk://task/{source_hash}"
    assert source_id not in record.source_url


def test_metadata_aliases_use_first_present_field_and_reject_null_identity() -> None:
    runner = RecordingRunner(
        [
            {
                "source_id": None,
                "taskId": "task-1",
                "source_title": "Preferred",
                "subject": "Fallback",
            }
        ]
    )

    with pytest.raises(DwsReadError) as error:
        read_task(
            runner,
            source("task", "task-1"),
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )

    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD


def test_calendar_lists_fixed_window_and_gets_only_unique_allowlisted_id() -> None:
    spec = source(
        "calendar",
        "event-2",
        window_start="2026-09-05T08:00:00+08:00",
        window_end="2026-09-05T18:00:00+08:00",
    )
    detail = {"eventId": "event-2", "summary": "复盘"}
    runner = RecordingRunner(
        [{"events": [{"eventId": "event-1"}, {"eventId": "event-2"}]}, detail]
    )

    record = read_calendar_event(
        runner,
        spec,
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    assert runner.calls == [
        (
            "calendar",
            "event",
            "list",
            "--start",
            "2026-09-05T08:00:00+08:00",
            "--end",
            "2026-09-05T18:00:00+08:00",
        ),
        ("calendar", "event", "get", "--id", "event-2"),
    ]
    assert record.content_text == canonical(detail)


def test_calendar_zero_match_is_deleted_without_get_and_duplicates_fail() -> None:
    spec = source(
        "calendar",
        "event-2",
        window_start="2026-09-05T08:00:00+08:00",
        window_end="2026-09-05T18:00:00+08:00",
    )
    missing = RecordingRunner([{"events": [{"eventId": "event-1"}]}])

    record = read_calendar_event(
        missing,
        spec,
        permission_scope="project:project-1",
        clock=lambda: NOW,
    )

    assert record.status == "deleted"
    assert len(missing.calls) == 1

    duplicate = RecordingRunner(
        [{"events": [{"eventId": "event-2"}, {"id": "event-2"}]}]
    )
    with pytest.raises(DwsReadError) as error:
        read_calendar_event(
            duplicate,
            spec,
            permission_scope="project:project-1",
            clock=lambda: NOW,
        )
    assert error.value.error_type is SourceErrorType.INVALID_PAYLOAD
    assert len(duplicate.calls) == 1


def test_record_and_bundle_are_frozen_and_enforce_status_invariants() -> None:
    content = "content"
    record = DwsSourceRecord(
        source_type="document",
        source_id="doc-1",
        permission_scope="project:project-1",
        fetched_at=NOW,
        status="active",
        source_title="Title",
        source_url="dingtalk://document/doc-1",
        source_version="1",
        source_time=NOW,
        content_text=content,
        attributes_json="{}",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        error_type=None,
        retryable=None,
        retry_after_seconds=None,
    )
    bundle = DwsSourceBundle(
        schema_version=1,
        project_id="project-1",
        project_name="Demo",
        permission_scope="project:project-1",
        collected_at=NOW,
        records=(record,),
        content_hash="a" * 64,
    )

    with pytest.raises(ValidationError):
        record.status = "failed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        bundle.project_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DwsSourceRecord(
            **{
                **record.model_dump(),
                "status": "failed",
                "error_type": "network_timeout",
                "retryable": True,
            }
        )
    with pytest.raises(ValidationError):
        DwsSourceRecord(**{**record.model_dump(), "content_hash": "b" * 64})
    with pytest.raises(ValidationError):
        DwsSourceBundle(
            **{
                **bundle.model_dump(),
                "content_hash": "a" * 65,
            }
        )

    failed = DwsSourceRecord(
        source_type="document",
        source_id="doc-1",
        permission_scope="project:project-1",
        fetched_at=NOW,
        status="failed",
        source_title="Known title",
        source_url="dingtalk://document/doc-1",
        error_type="network_timeout",
        retryable=True,
    )
    assert failed.source_title == "Known title"


def test_collect_sources_isolates_error_and_omits_profile() -> None:
    project = DwsProjectManifest(
        project_id="project-1",
        project_name="Demo",
        profile="private-profile",
        permission_scope="project:project-1",
        sources=(
            source("task", "task-1"),
            source("task", "task-2"),
        ),
    )

    class PartiallyFailingRunner:
        def run(self, args: tuple[str, ...]) -> dict[str, object]:
            if args[-1] == "task-2":
                raise DwsReadError(SourceErrorType.PERMISSION_DENIED, False)
            return {"taskId": "task-1", "subject": "Done"}

    bundle = collect_sources(project, PartiallyFailingRunner(), clock=lambda: NOW)

    assert [record.status for record in bundle.records] == ["active", "revoked"]
    serialized = bundle.model_dump_json()
    assert "private-profile" not in serialized
    assert "profile" not in serialized
