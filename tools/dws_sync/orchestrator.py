from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from companion_gateway.project.protection import ContentProtector
from tools.dws_project_sync import CommandResult


class Dispatch(Protocol):
    def __call__(
        self,
        root: Path,
        command: str,
        run_token: str | None,
        dry_run: bool,
        protector: ContentProtector,
        **kwargs: object,
    ) -> CommandResult: ...


def _runtime_dispatch(
    root: Path,
    command: str,
    run_token: str | None,
    dry_run: bool,
    protector: ContentProtector,
    **kwargs: object,
) -> CommandResult:
    from tools.dws_sync_runtime import dispatch_result

    return dispatch_result(root, command, run_token, dry_run, protector, **kwargs)


@dataclass(frozen=True)
class SyncRunResult:
    status: str
    stage: str
    project_id: str | None
    outcome: str | None = None
    project_status: str | None = None
    active_sources: int = 0
    failed_sources: int = 0
    accepted_sources: int = 0
    error_type: str | None = None
    manual_refresh_required: bool = False
    rerun_count: int = 0
    release_status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _SyncDetails:
    project_id: str | None = None
    outcome: str | None = None
    project_status: str | None = None
    active_sources: int = 0
    failed_sources: int = 0
    accepted_sources: int = 0


def _text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _count(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _details(
    payload: Mapping[str, object],
    previous: _SyncDetails,
) -> _SyncDetails:
    return _SyncDetails(
        project_id=_text(payload, "project_id") or previous.project_id,
        outcome=_text(payload, "outcome") or previous.outcome,
        project_status=_text(payload, "project_status") or previous.project_status,
        active_sources=(
            _count(payload, "active_sources")
            if "active_sources" in payload
            else previous.active_sources
        ),
        failed_sources=(
            _count(payload, "failed_sources")
            if "failed_sources" in payload
            else previous.failed_sources
        ),
        accepted_sources=(
            _count(payload, "accepted_sources")
            if "accepted_sources" in payload
            else previous.accepted_sources
        ),
    )


def _result(
    status: str,
    stage: str,
    details: _SyncDetails,
    *,
    error_type: str | None = None,
    manual_refresh_required: bool = False,
    rerun_count: int = 0,
    release_status: str | None = None,
) -> SyncRunResult:
    return SyncRunResult(
        status=status,
        stage=stage,
        project_id=details.project_id,
        outcome=details.outcome,
        project_status=details.project_status,
        active_sources=details.active_sources,
        failed_sources=details.failed_sources,
        accepted_sources=details.accepted_sources,
        error_type=error_type,
        manual_refresh_required=manual_refresh_required,
        rerun_count=rerun_count,
        release_status=release_status,
    )


def _error_type(payload: Mapping[str, object]) -> str:
    return _text(payload, "error_type") or "sync_failed"


def _dispatch(
    dispatch: Dispatch,
    root: Path,
    command: str,
    run_token: str | None,
    protector: ContentProtector,
    **kwargs: object,
) -> CommandResult:
    return dispatch(root, command, run_token, False, protector, **kwargs)


def _release(
    dispatch: Dispatch,
    root: Path,
    run_token: str,
    protector: ContentProtector,
) -> str:
    try:
        released = _dispatch(dispatch, root, "abort", run_token, protector)
    except BaseException:
        return "failed"
    if released.exit_code != 0:
        return "failed"
    return _text(released.payload, "status") or "aborted"


def _failure(
    dispatch: Dispatch,
    root: Path,
    run_token: str,
    protector: ContentProtector,
    stage: str,
    details: _SyncDetails,
    error_type: str,
    rerun_count: int,
) -> SyncRunResult:
    return _result(
        "failed",
        stage,
        details,
        error_type=error_type,
        rerun_count=rerun_count,
        release_status=_release(dispatch, root, run_token, protector),
    )


def _run_round(
    dispatch: Dispatch,
    root: Path,
    run_token: str,
    protector: ContentProtector,
    details: _SyncDetails,
    rerun_count: int,
) -> tuple[SyncRunResult | None, _SyncDetails, str | None]:
    for command, expected, options in (
        ("collect-direct", "collected", {}),
        ("pending", "pending_fetched", {}),
        ("reuse-artifact", "artifact_reused", {"unattended": True}),
        ("push", "synced", {}),
        ("end", "completed", {}),
    ):
        try:
            dispatched = _dispatch(
                dispatch, root, command, run_token, protector, **options
            )
        except (KeyboardInterrupt, SystemExit):
            return (
                _failure(
                    dispatch,
                    root,
                    run_token,
                    protector,
                    command,
                    details,
                    "interrupted",
                    rerun_count,
                ),
                details,
                None,
            )
        except Exception:
            return (
                _failure(
                    dispatch,
                    root,
                    run_token,
                    protector,
                    command,
                    details,
                    "sync_failed",
                    rerun_count,
                ),
                details,
                None,
            )

        payload = dispatched.payload
        details = _details(payload, details)
        status = _text(payload, "status")
        if dispatched.exit_code != 0:
            return (
                _failure(
                    dispatch,
                    root,
                    run_token,
                    protector,
                    command,
                    details,
                    _error_type(payload),
                    rerun_count,
                ),
                details,
                None,
            )
        if command == "reuse-artifact" and status in {
            "manual_refresh_required",
            "artifact_required",
        }:
            return (
                _result(
                    "awaiting_artifact",
                    command,
                    details,
                    manual_refresh_required=True,
                    rerun_count=rerun_count,
                    release_status=_release(dispatch, root, run_token, protector),
                ),
                details,
                None,
            )
        if command == "end" and status == "rerun":
            next_token = _text(payload, "run_token")
            if rerun_count == 0 and next_token:
                return None, details, next_token
            return (
                _failure(
                    dispatch,
                    root,
                    run_token,
                    protector,
                    command,
                    details,
                    "unexpected_status",
                    rerun_count,
                ),
                details,
                None,
            )
        if status != expected:
            return (
                _failure(
                    dispatch,
                    root,
                    run_token,
                    protector,
                    command,
                    details,
                    "unexpected_status",
                    rerun_count,
                ),
                details,
                None,
            )

    return _result("completed", "end", details, rerun_count=rerun_count), details, None


def run_once(
    root: Path,
    protector: ContentProtector,
    dispatch: Dispatch = _runtime_dispatch,
) -> SyncRunResult:
    details = _SyncDetails()
    try:
        begun = _dispatch(dispatch, root, "begin", None, protector)
    except (KeyboardInterrupt, SystemExit):
        return _result("failed", "begin", details, error_type="interrupted")
    except Exception:
        return _result("failed", "begin", details, error_type="sync_failed")

    details = _details(begun.payload, details)
    status = _text(begun.payload, "status")
    if begun.exit_code != 0:
        return _result("failed", "begin", details, error_type=_error_type(begun.payload))
    if status in {"coalesced", "completed"}:
        return _result(status, "begin", details)
    run_token = _text(begun.payload, "run_token")
    if status != "started" or not run_token:
        return _result("failed", "begin", details, error_type="unexpected_status")

    rerun_count = 0
    while True:
        final, details, rerun_token = _run_round(
            dispatch,
            root,
            run_token,
            protector,
            details,
            rerun_count,
        )
        if final is not None:
            return final
        assert rerun_token is not None
        run_token = rerun_token
        rerun_count += 1
