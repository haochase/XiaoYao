from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen as default_urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.project.index import chunk_text
from companion_gateway.project.models import EvidenceRef, ProjectContextPackage
from companion_gateway.project.sync_models import (
    SourceSnapshot,
    SourceTombstone,
    SyncEnvelope,
)
from companion_gateway.project.sync_service import compute_envelope_content_hash

try:
    from tools.dws_sync import (
        DwsCommandRunner,
        DwsManifest,
        DwsProjectManifest,
        DwsSourceBundle,
        DwsSourceRecord,
        collect_sources,
    )
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from dws_sync import (  # type: ignore[no-redef]
        DwsCommandRunner,
        DwsManifest,
        DwsProjectManifest,
        DwsSourceBundle,
        DwsSourceRecord,
        collect_sources,
    )


MAX_PAYLOAD_BYTES = 2_097_152
TOKEN_ENVIRONMENT_VARIABLE = "COMPANION_DWS_SYNC_TOKEN"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESPONSE_KEYS = {
    "sync_id",
    "outcome",
    "project_status",
    "accepted_sources",
    "failed_sources",
    "generation_id",
    "next_sync_before",
}
_PUBLIC_ERROR_TYPES = {
    "arguments_invalid",
    "authentication_failed",
    "context_file_invalid",
    "context_file_not_absolute",
    "context_file_parent_invalid",
    "context_mismatch",
    "dws_path_not_absolute",
    "dws_path_not_regular_file",
    "gateway_invalid",
    "http_error",
    "invalid_payload",
    "manifest_invalid_json",
    "manifest_invalid_utf8",
    "manifest_not_absolute",
    "manifest_not_found",
    "manifest_not_regular_file",
    "manifest_parent_invalid",
    "manifest_root_invalid",
    "manifest_too_large",
    "manifest_unreadable",
    "manifest_validation_failed",
    "network_error",
    "network_timeout",
    "now_not_timezone_aware",
    "output_not_absolute",
    "output_parent_invalid",
    "payload_too_large",
    "pending_sync_conflict",
    "permission_denied",
    "private_file_write_failed",
    "project_not_found",
    "provider_unavailable",
    "rate_limited",
    "response_invalid",
    "response_sync_mismatch",
    "source_bundle_hash_mismatch",
    "source_bundle_mismatch",
    "source_excerpt_mismatch",
    "source_ref_mismatch",
    "source_status_invalid",
    "sources_file_invalid",
    "sources_file_not_absolute",
    "sources_file_parent_invalid",
    "state_file_invalid",
    "state_file_not_absolute",
    "state_file_parent_invalid",
    "state_project_mismatch",
    "sync_failed",
    "token_invalid",
    "token_missing",
    "unknown",
}


def _safe_id(value: str, field_name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name}_invalid")
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_non_finite(_value: str) -> None:
    raise ValueError("non_finite_json")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class QwenProjectContextArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    context: ProjectContextPackage
    completed_retrieval_request_ids: tuple[str, ...] = ()

    @field_validator("completed_retrieval_request_ids")
    @classmethod
    def validate_request_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("retrieval_request_ids_not_unique")
        for value in values:
            _safe_id(value, "retrieval_request_id")
        return values


class PendingSync(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_cursor: int = Field(ge=1)
    content_hash: str
    sync_id: str

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("content_hash_invalid")
        return value

    @field_validator("sync_id")
    @classmethod
    def validate_sync_id(cls, value: str) -> str:
        return _safe_id(value, "sync_id")


class SyncCliState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    project_id: str
    last_cursor: int = Field(ge=0)
    last_content_hash: str | None
    last_sync_id: str | None
    pending: PendingSync | None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _safe_id(value, "project_id")

    @field_validator("last_content_hash")
    @classmethod
    def validate_last_content_hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("last_content_hash_invalid")
        return value

    @field_validator("last_sync_id")
    @classmethod
    def validate_last_sync_id(cls, value: str | None) -> str | None:
        return _safe_id(value, "last_sync_id") if value is not None else None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "SyncCliState":
        has_last = self.last_content_hash is not None and self.last_sync_id is not None
        if self.last_cursor == 0 and (
            self.last_content_hash is not None or self.last_sync_id is not None
        ):
            raise ValueError("state_last_invalid")
        if self.last_cursor > 0 and not has_last:
            raise ValueError("state_last_invalid")
        if (
            self.pending is not None
            and self.pending.source_cursor != self.last_cursor + 1
        ):
            raise ValueError("state_pending_cursor_invalid")
        return self


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="dws_project_sync", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect")
    collect.add_argument("--manifest", required=True)
    collect.add_argument("--project", required=True)
    collect.add_argument("--dws-path", required=True)
    collect.add_argument("--output", required=True)

    push = commands.add_parser("push")
    push.add_argument("--manifest", required=True)
    push.add_argument("--project", required=True)
    push.add_argument("--sources-file", required=True)
    push.add_argument("--context-file", required=True)
    push.add_argument("--state-file", required=True)
    push.add_argument("--gateway", required=True)
    push.add_argument("--dry-run", action="store_true")
    return parser


def _absolute_private_path(raw_path: str, label: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{label}_not_absolute")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError(f"{label}_parent_invalid")
    return path


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError(f"{label}_unreadable") from None
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError(f"{label}_invalid") from None
    if not isinstance(payload, dict):
        raise ValueError(f"{label}_invalid")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor = -1
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError:
        raise ValueError("private_file_write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _selected_project(manifest: DwsManifest, project_id: str) -> DwsProjectManifest:
    matches = [item for item in manifest.projects if item.project_id == project_id]
    if len(matches) != 1:
        raise ValueError("project_not_found")
    return matches[0]


def _validate_bundle(
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
) -> None:
    if (
        source_bundle.project_id != project.project_id
        or source_bundle.project_name != project.project_name
        or source_bundle.permission_scope != project.permission_scope
    ):
        raise ValueError("source_bundle_mismatch")
    expected = {(item.source_type, item.source_id) for item in project.sources}
    actual = {(item.source_type, item.source_id) for item in source_bundle.records}
    if actual != expected:
        raise ValueError("source_bundle_mismatch")
    payload = {
        "schema_version": source_bundle.schema_version,
        "project_id": source_bundle.project_id,
        "project_name": source_bundle.project_name,
        "permission_scope": source_bundle.permission_scope,
        "collected_at": source_bundle.collected_at.isoformat(),
        "records": [
            record.model_dump(mode="json") for record in source_bundle.records
        ],
    }
    if _sha256(_canonical_bytes(payload).decode("utf-8")) != source_bundle.content_hash:
        raise ValueError("source_bundle_hash_mismatch")


def _normalized_text(value: str) -> str:
    return "".join(value.split()).casefold()


def _all_references(context: ProjectContextPackage) -> tuple[EvidenceRef, ...]:
    nested = tuple(
        source
        for decision in context.active_decisions
        for source in decision.source_refs
    )
    return (*context.source_refs, *nested)


def _validate_context(
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
    context: ProjectContextPackage,
) -> None:
    if (
        context.project_id != project.project_id
        or context.project_name != project.project_name
        or context.permission_scope != project.permission_scope
    ):
        raise ValueError("context_mismatch")
    active = {
        (record.source_type.value, record.source_id): record
        for record in source_bundle.records
        if record.status == "active"
    }
    for source_ref in _all_references(context):
        record = active.get((source_ref.source_type, source_ref.source_id))
        if record is None or (
            source_ref.permission_scope != record.permission_scope
            or source_ref.source_title != record.source_title
            or source_ref.source_url != record.source_url
            or source_ref.source_time != record.source_time
        ):
            raise ValueError("source_ref_mismatch")
        normalized_content = _normalized_text(record.content_text or "")
        normalized_excerpt = _normalized_text(source_ref.excerpt)
        if not normalized_content or normalized_excerpt not in normalized_content:
            raise ValueError("source_excerpt_mismatch")


def _source_snapshot(record: DwsSourceRecord) -> SourceSnapshot:
    permission_hash = _sha256(record.permission_scope)
    if record.status == "active":
        assert record.source_title is not None
        assert record.source_url is not None
        assert record.source_version is not None
        assert record.source_time is not None
        assert record.content_text is not None
        assert record.content_hash is not None
        return SourceSnapshot(
            source_type=record.source_type,
            source_id=record.source_id,
            source_title=record.source_title,
            source_url=record.source_url,
            source_version=record.source_version,
            source_time=record.source_time,
            fetched_at=record.fetched_at,
            permission_scope=record.permission_scope,
            permission_hash=permission_hash,
            status="active",
            chunks=chunk_text(
                record.source_id,
                record.source_version,
                record.content_text,
            ),
            content_hash=record.content_hash,
        )
    if record.status != "failed":
        raise ValueError("source_status_invalid")
    source_id_hash = _sha256(record.source_id)
    return SourceSnapshot(
        source_type=record.source_type,
        source_id=record.source_id,
        source_title=f"{record.source_type.value}:{source_id_hash[:12]}",
        source_url=f"dingtalk://{record.source_type.value}/{source_id_hash}",
        source_version=None,
        source_time=None,
        fetched_at=record.fetched_at,
        permission_scope=record.permission_scope,
        permission_hash=permission_hash,
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=record.error_type,
        retryable=record.retryable,
        retry_after_seconds=record.retry_after_seconds,
    )


def _sync_id(project_id: str, cursor: int, content_hash: str) -> str:
    material = f"{project_id}\0{cursor}\0{content_hash}"
    return "sync_" + _sha256(material)[:32]


def _build_envelope(
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
    context: ProjectContextPackage,
    *,
    completed_retrieval_request_ids: tuple[str, ...],
    source_cursor: int,
    now: datetime,
) -> SyncEnvelope:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now_not_timezone_aware")
    _validate_bundle(project, source_bundle)
    _validate_context(project, source_bundle, context)
    sources = tuple(
        _source_snapshot(record)
        for record in source_bundle.records
        if record.status in {"active", "failed"}
    )
    tombstones = tuple(
        SourceTombstone(
            source_type=record.source_type,
            source_id=record.source_id,
            status=record.status,
            occurred_at=record.fetched_at,
            permission_scope=record.permission_scope,
        )
        for record in source_bundle.records
        if record.status in {"deleted", "revoked"}
    )
    draft = SyncEnvelope(
        schema_version=1,
        sync_id="sync_" + "0" * 32,
        project_id=project.project_id,
        generated_at=context.generated_at,
        source_cursor=source_cursor,
        content_hash="0" * 64,
        producer="qwenwork-dws",
        context=context,
        sources=sources,
        tombstones=tombstones,
        completed_retrieval_request_ids=completed_retrieval_request_ids,
    )
    content_hash = compute_envelope_content_hash(draft)
    return draft.model_copy(
        update={
            "content_hash": content_hash,
            "sync_id": _sync_id(project.project_id, source_cursor, content_hash),
        }
    )


def build_envelope(
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
    context: ProjectContextPackage,
    *,
    now: datetime,
) -> SyncEnvelope:
    return _build_envelope(
        project,
        source_bundle,
        context,
        completed_retrieval_request_ids=(),
        source_cursor=1,
        now=now,
    )


def _initial_state(project_id: str) -> SyncCliState:
    return SyncCliState(
        schema_version=1,
        project_id=project_id,
        last_cursor=0,
        last_content_hash=None,
        last_sync_id=None,
        pending=None,
    )


def _load_state(path: Path, project_id: str) -> SyncCliState:
    if not path.exists():
        return _initial_state(project_id)
    try:
        state = SyncCliState.model_validate(_read_json_object(path, "state_file"))
    except (TypeError, ValueError):
        raise ValueError("state_file_invalid") from None
    if state.project_id != project_id:
        raise ValueError("state_project_mismatch")
    return state


def _gateway_base(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("gateway_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port != 8731
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("gateway_invalid")
    return f"http://{parsed.hostname}:8731"


def _validate_response(
    raw: bytes,
    *,
    envelope: SyncEnvelope,
) -> dict[str, object]:
    if len(raw) > 65_536:
        raise ValueError("response_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError("response_invalid") from None
    if not isinstance(payload, dict) or set(payload) != _RESPONSE_KEYS:
        raise ValueError("response_invalid")
    if payload["sync_id"] != envelope.sync_id:
        raise ValueError("response_sync_mismatch")
    if payload["outcome"] not in {"applied", "unchanged", "degraded"}:
        raise ValueError("response_invalid")
    if payload["project_status"] not in {
        "healthy",
        "degraded",
        "stale",
        "clock_untrusted",
    }:
        raise ValueError("response_invalid")
    accepted = payload["accepted_sources"]
    failed = payload["failed_sources"]
    if (
        isinstance(accepted, bool)
        or not isinstance(accepted, int)
        or accepted < 0
        or isinstance(failed, bool)
        or not isinstance(failed, int)
        or failed < 0
        or accepted + failed != len(envelope.sources) + len(envelope.tombstones)
    ):
        raise ValueError("response_invalid")
    generation_id = payload["generation_id"]
    if generation_id is not None and (
        not isinstance(generation_id, str) or not generation_id.strip()
    ):
        raise ValueError("response_invalid")
    next_sync = payload["next_sync_before"]
    if not isinstance(next_sync, str):
        raise ValueError("response_invalid")
    try:
        parsed_next_sync = datetime.fromisoformat(next_sync.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("response_invalid") from None
    if parsed_next_sync.tzinfo is None or parsed_next_sync.utcoffset() is None:
        raise ValueError("response_invalid")
    return payload


def _emit(payload: Mapping[str, object]) -> None:
    sys.stdout.buffer.write(_canonical_bytes(dict(payload)) + b"\n")
    sys.stdout.buffer.flush()


def _collect_command(
    args: argparse.Namespace,
    *,
    runner: object | None,
    now: Callable[[], datetime],
) -> dict[str, object]:
    manifest_path = _absolute_private_path(args.manifest, "manifest")
    output_path = _absolute_private_path(args.output, "output")
    dws_path = Path(args.dws_path)
    manifest = DwsManifest.load(manifest_path)
    project = _selected_project(manifest, args.project)
    actual_runner = runner
    if actual_runner is None:
        actual_runner = DwsCommandRunner(dws_path, profile=project.profile)
    source_bundle = collect_sources(project, actual_runner, clock=now)
    encoded = _canonical_bytes(source_bundle.model_dump(mode="json"))
    _atomic_write(output_path, encoded)
    return {
        "status": "collected",
        "project_id": project.project_id,
        "source_count": len(source_bundle.records),
        "active_sources": sum(
            item.status == "active" for item in source_bundle.records
        ),
        "failed_sources": sum(
            item.status == "failed" for item in source_bundle.records
        ),
        "content_hash": source_bundle.content_hash,
        "output_bytes": len(encoded),
    }


def _read_push_inputs(
    args: argparse.Namespace,
) -> tuple[DwsProjectManifest, DwsSourceBundle, QwenProjectContextArtifact, Path]:
    manifest_path = _absolute_private_path(args.manifest, "manifest")
    sources_path = _absolute_private_path(args.sources_file, "sources_file")
    context_path = _absolute_private_path(args.context_file, "context_file")
    state_path = _absolute_private_path(args.state_file, "state_file")
    manifest = DwsManifest.load(manifest_path)
    project = _selected_project(manifest, args.project)
    try:
        source_bundle = DwsSourceBundle.model_validate(
            _read_json_object(sources_path, "sources_file")
        )
    except (TypeError, ValueError):
        raise ValueError("sources_file_invalid") from None
    try:
        artifact = QwenProjectContextArtifact.model_validate(
            _read_json_object(context_path, "context_file")
        )
    except (TypeError, ValueError):
        raise ValueError("context_file_invalid") from None
    return project, source_bundle, artifact, state_path


def _push_command(
    args: argparse.Namespace,
    *,
    opener: Callable[..., object],
    environ: Mapping[str, str],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
) -> dict[str, object]:
    gateway = _gateway_base(args.gateway)
    project, source_bundle, artifact, state_path = _read_push_inputs(args)

    if args.dry_run:
        envelope = _build_envelope(
            project,
            source_bundle,
            artifact.context,
            completed_retrieval_request_ids=(
                artifact.completed_retrieval_request_ids
            ),
            source_cursor=1,
            now=now(),
        )
        encoded = _canonical_bytes(envelope.model_dump(mode="json"))
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload_too_large")
        return {
            "status": "ready",
            "project_id": project.project_id,
            "source_count": len(source_bundle.records),
            "payload_bytes": len(encoded),
            "content_hash": envelope.content_hash,
        }

    token = environ.get(TOKEN_ENVIRONMENT_VARIABLE, "")
    if not token:
        raise ValueError("token_missing")
    if "\r" in token or "\n" in token:
        raise ValueError("token_invalid")
    state = _load_state(state_path, project.project_id)
    cursor = (
        state.pending.source_cursor
        if state.pending is not None
        else state.last_cursor + 1
    )
    envelope = _build_envelope(
        project,
        source_bundle,
        artifact.context,
        completed_retrieval_request_ids=artifact.completed_retrieval_request_ids,
        source_cursor=cursor,
        now=now(),
    )
    if state.pending is not None:
        if state.pending.content_hash != envelope.content_hash:
            raise ValueError("pending_sync_conflict")
        if state.pending.sync_id != envelope.sync_id:
            raise ValueError("state_file_invalid")
    encoded = _canonical_bytes(envelope.model_dump(mode="json"))
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload_too_large")

    pending = PendingSync(
        source_cursor=envelope.source_cursor,
        content_hash=envelope.content_hash,
        sync_id=envelope.sync_id,
    )
    if state.pending is None:
        _atomic_write(
            state_path,
            _canonical_bytes(
                state.model_copy(update={"pending": pending}).model_dump()
            ),
        )

    request = Request(
        (
            f"{gateway}/v1/projects/"
            f"{quote(project.project_id, safe='')}/sync"
        ),
        data=encoded,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
        },
        method="POST",
    )
    started = monotonic()
    response = None
    try:
        response = opener(request, timeout=30.0)
        raw_response = response.read()
    except HTTPError:
        raise ValueError("http_error") from None
    except (TimeoutError, URLError, OSError):
        raise ValueError("network_error") from None
    except Exception:
        raise ValueError("network_error") from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    response_payload = _validate_response(raw_response, envelope=envelope)
    duration_ms = max(0, int((monotonic() - started) * 1000))
    promoted = SyncCliState(
        schema_version=1,
        project_id=project.project_id,
        last_cursor=envelope.source_cursor,
        last_content_hash=envelope.content_hash,
        last_sync_id=envelope.sync_id,
        pending=None,
    )
    _atomic_write(state_path, _canonical_bytes(promoted.model_dump()))
    return {
        "status": "synced",
        "project_id": project.project_id,
        "source_count": len(source_bundle.records),
        "payload_bytes": len(encoded),
        "content_hash": envelope.content_hash,
        "outcome": response_payload["outcome"],
        "project_status": response_payload["project_status"],
        "accepted_sources": response_payload["accepted_sources"],
        "failed_sources": response_payload["failed_sources"],
        "duration_ms": duration_ms,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: object | None = None,
    urlopen: Callable[..., object] = default_urlopen,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.perf_counter,
) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "collect":
            output = _collect_command(args, runner=runner, now=now)
        else:
            output = _push_command(
                args,
                opener=urlopen,
                environ=os.environ if environ is None else environ,
                now=now,
                monotonic=monotonic,
            )
    except (KeyboardInterrupt, SystemExit):
        output = {"status": "error", "error_type": "interrupted"}
        result = 1
    except Exception as exc:
        label = str(exc)
        if label not in _PUBLIC_ERROR_TYPES:
            label = "sync_failed"
        output = {"status": "error", "error_type": label}
        result = 1
    else:
        result = 0
    _emit(output)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
