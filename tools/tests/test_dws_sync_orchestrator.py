from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.dws_project_sync import CommandResult


def result(status: str, **payload: object) -> CommandResult:
    return CommandResult(
        0, {"status": status, "project_id": "project-1", **payload}
    )


def run_with(
    responses: list[CommandResult],
) -> tuple[object, list[tuple[str, str | None, bool, dict[str, object]]]]:
    from tools.dws_sync.orchestrator import run_once

    observed: list[tuple[str, str | None, bool, dict[str, object]]] = []

    def dispatch(
        _root: Path,
        command: str,
        run_token: str | None,
        dry_run: bool,
        _protector: object,
        **kwargs: object,
    ) -> CommandResult:
        observed.append((command, run_token, dry_run, kwargs))
        return responses.pop(0)

    return run_once(Path("fixed-root"), object(), dispatch=dispatch), observed


def test_run_once_completes_unchanged_sync_without_exposing_token() -> None:
    output, observed = run_with(
        [
            result("started", run_token="private-token"),
            result("collected", active_sources=2, failed_sources=0),
            result("pending_fetched"),
            result("artifact_reused"),
            result(
                "synced",
                outcome="applied",
                project_status="healthy",
                accepted_sources=2,
                failed_sources=0,
            ),
            result("completed", run_token=None),
        ]
    )

    assert output.status == "completed"
    assert output.stage == "end"
    assert output.project_id == "project-1"
    assert output.outcome == "applied"
    assert output.project_status == "healthy"
    assert output.active_sources == 2
    assert output.failed_sources == 0
    assert output.accepted_sources == 2
    assert output.error_type is None
    assert not output.manual_refresh_required
    assert output.rerun_count == 0
    assert [item[:3] for item in observed] == [
        ("begin", None, False),
        ("collect-direct", "private-token", False),
        ("pending", "private-token", False),
        ("reuse-artifact", "private-token", False),
        ("push", "private-token", False),
        ("end", "private-token", False),
    ]
    assert observed[3][3] == {"unattended": True}
    assert "private-token" not in json.dumps(output.to_dict())


@pytest.mark.parametrize(
    "stage", ("collect-direct", "pending", "reuse-artifact", "push", "end")
)
def test_run_once_aborts_same_token_after_each_started_stage_failure(stage: str) -> None:
    stages = ("collect-direct", "pending", "reuse-artifact", "push", "end")
    responses = [result("started", run_token="private-token")]
    for current in stages:
        if current == stage:
            responses.append(
                CommandResult(
                    1, {"status": "error", "error_type": "sync_failed"}
                )
            )
            break
        if current == "collect-direct":
            responses.append(result("collected", active_sources=1, failed_sources=0))
        elif current == "pending":
            responses.append(result("pending_fetched"))
        elif current == "reuse-artifact":
            responses.append(result("artifact_reused"))
        elif current == "push":
            responses.append(result("synced", outcome="applied", project_status="healthy"))
    responses.append(result("aborted"))

    output, observed = run_with(responses)

    assert output.status == "failed"
    assert output.stage == stage
    assert output.error_type == "sync_failed"
    assert observed[-1][:3] == ("abort", "private-token", False)
    assert "private-token" not in json.dumps(output.to_dict())


@pytest.mark.parametrize(
    "reuse_status", ("manual_refresh_required", "artifact_required")
)
def test_run_once_aborts_for_manual_refresh_without_claiming_sync(reuse_status: str) -> None:
    output, observed = run_with(
        [
            result("started", run_token="private-token"),
            result("collected", active_sources=1, failed_sources=0),
            result("pending_fetched"),
            result(reuse_status),
            result("aborted"),
        ]
    )

    assert output.status == "awaiting_artifact"
    assert output.stage == "reuse-artifact"
    assert output.manual_refresh_required
    assert output.error_type is None
    assert [item[0] for item in observed] == [
        "begin", "collect-direct", "pending", "reuse-artifact", "abort"
    ]


def test_run_once_coalesced_does_not_dispatch_follow_up_commands() -> None:
    output, observed = run_with([result("coalesced")])

    assert output.status == "coalesced"
    assert output.stage == "begin"
    assert observed == [("begin", None, False, {})]


def test_run_once_repeats_full_lifecycle_once_for_end_rerun() -> None:
    output, observed = run_with(
        [
            result("started", run_token="first-token"),
            result("collected", active_sources=1, failed_sources=0),
            result("pending_fetched"),
            result("artifact_reused"),
            result(
                "synced",
                outcome="applied",
                project_status="healthy",
                accepted_sources=1,
                failed_sources=0,
            ),
            result("rerun", run_token="second-token"),
            result("collected", active_sources=2, failed_sources=0),
            result("pending_fetched"),
            result("artifact_reused"),
            result(
                "synced",
                outcome="applied",
                project_status="healthy",
                accepted_sources=2,
                failed_sources=0,
            ),
            result("completed", run_token=None),
        ]
    )

    assert output.status == "completed"
    assert output.rerun_count == 1
    assert [item[0] for item in observed] == [
        "begin",
        "collect-direct",
        "pending",
        "reuse-artifact",
        "push",
        "end",
        "collect-direct",
        "pending",
        "reuse-artifact",
        "push",
        "end",
    ]
    assert {item[1] for item in observed[1:6]} == {"first-token"}
    assert {item[1] for item in observed[6:]} == {"second-token"}


def test_run_once_aborts_second_rerun_as_an_unexpected_lifecycle_state() -> None:
    output, observed = run_with(
        [
            result("started", run_token="first-token"),
            result("collected"),
            result("pending_fetched"),
            result("artifact_reused"),
            result("synced"),
            result("rerun", run_token="second-token"),
            result("collected"),
            result("pending_fetched"),
            result("artifact_reused"),
            result("synced"),
            result("rerun", run_token="third-token"),
            result("aborted"),
        ]
    )

    assert output.status == "failed"
    assert output.stage == "end"
    assert output.error_type == "unexpected_status"
    assert output.rerun_count == 1
    assert observed[-1][:3] == ("abort", "second-token", False)


def test_run_once_preserves_main_error_when_abort_fails() -> None:
    output, observed = run_with(
        [
            result("started", run_token="private-token"),
            CommandResult(1, {"status": "error", "error_type": "sync_failed"}),
            CommandResult(1, {"status": "error", "error_type": "run_stage_invalid"}),
        ]
    )

    assert output.status == "failed"
    assert output.error_type == "sync_failed"
    assert output.release_status == "failed"
    assert observed[-1][0] == "abort"


def test_run_once_aborts_after_interrupt() -> None:
    from tools.dws_sync.orchestrator import run_once

    observed: list[tuple[str, str | None]] = []

    def dispatch(
        _root: Path,
        command: str,
        run_token: str | None,
        _dry_run: bool,
        _protector: object,
        **_kwargs: object,
    ) -> CommandResult:
        observed.append((command, run_token))
        if command == "begin":
            return result("started", run_token="private-token")
        if command == "collect-direct":
            raise KeyboardInterrupt
        assert command == "abort"
        return result("aborted")

    output = run_once(Path("fixed-root"), object(), dispatch=dispatch)

    assert output.status == "failed"
    assert output.error_type == "interrupted"
    assert observed == [
        ("begin", None),
        ("collect-direct", "private-token"),
        ("abort", "private-token"),
    ]
