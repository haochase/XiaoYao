from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from companion_gateway.project.index import chunk_text
from companion_gateway.project.models import EvidenceRef, ProjectContextPackage
from companion_gateway.project.sync_models import (
    ClaimedRetrievalRequest,
    RetrievalCompletionClaim,
    RetrievalRequestStatus,
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
        DwsRetrievalRequest,
        DwsRetrievalSource,
        DwsSourceBundle,
        DwsSourceRecord,
        collect_sources,
    )
    from tools.dws_sync import lifecycle
    from tools.dws_sync.host_bridge import (
        MAX_HOST_IMPORT_BYTES,
        import_single_document_bundle,
    )
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from dws_sync import (  # type: ignore[no-redef]
        DwsCommandRunner,
        DwsManifest,
        DwsProjectManifest,
        DwsRetrievalRequest,
        DwsRetrievalSource,
        DwsSourceBundle,
        DwsSourceRecord,
        collect_sources,
    )
    from dws_sync import lifecycle  # type: ignore[no-redef]
    from dws_sync.host_bridge import (  # type: ignore[no-redef]
        MAX_HOST_IMPORT_BYTES,
        import_single_document_bundle,
    )


MAX_PAYLOAD_BYTES = 2_097_152
MAX_PRIVATE_INPUT_BYTES = 2_097_152
MAX_STATE_BYTES = 65_536
SYNC_LOCK_TIMEOUT_SECONDS = 30.0
LIFECYCLE_ROOT = lifecycle.state_lock.PRIVATE_LOCK_ROOT
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
    "context_file_too_large",
    "context_fact_unreferenced",
    "context_mismatch",
    "dws_path_not_absolute",
    "dws_path_not_regular_file",
    "gateway_invalid",
    "http_error",
    "host_handoff_required",
    "host_import_invalid",
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
    "retrieval_request_invalid",
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
    "sources_file_too_large",
    "state_file_invalid",
    "state_file_not_absolute",
    "state_file_parent_invalid",
    "state_file_too_large",
    "state_project_mismatch",
    "sync_lock_timeout",
    "sync_failed",
    "token_invalid",
    "token_missing",
    "lifecycle_state_invalid",
    "lifecycle_active",
    "run_stage_invalid",
    "run_token_invalid",
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

    @field_validator("context")
    @classmethod
    def validate_context_facts(
        cls,
        context: ProjectContextPackage,
    ) -> ProjectContextPackage:
        _require_sourced_context(context)
        return context

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
    completion_claims_hash: str = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"

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

    @field_validator("completion_claims_hash")
    @classmethod
    def validate_claims_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("completion_claims_hash_invalid")
        return value


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
    parser = _ArgumentParser(prog="dws_project_sync", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", add_help=False)
    collect.add_argument("--manifest", required=True)
    collect.add_argument("--project", required=True)
    collect.add_argument("--dws-path", required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--run-token")

    host_import = commands.add_parser("host-import", add_help=False)
    host_import.add_argument("--manifest", required=True)
    host_import.add_argument("--project", required=True)
    host_import.add_argument("--output", required=True)
    host_import.add_argument("--run-token", required=True)

    pending = commands.add_parser("pending", add_help=False)
    pending.add_argument("--manifest", required=True)
    pending.add_argument("--project", required=True)
    pending.add_argument("--sources-file", required=True)
    pending.add_argument("--gateway", required=True)
    pending.add_argument("--run-token")

    artifact = commands.add_parser("artifact", add_help=False)
    artifact.add_argument("--project", required=True)
    artifact.add_argument("--context-file", required=True)
    artifact.add_argument("--run-token", required=True)

    push = commands.add_parser("push", add_help=False)
    push.add_argument("--manifest", required=True)
    push.add_argument("--project", required=True)
    push.add_argument("--sources-file", required=True)
    push.add_argument("--context-file", required=True)
    push.add_argument("--state-file", required=True)
    push.add_argument("--gateway", required=True)
    push.add_argument("--dry-run", action="store_true")
    push.add_argument("--run-token")

    begin = commands.add_parser("begin", add_help=False)
    begin.add_argument("--project", required=True)
    for command in ("end", "abort"):
        terminal = commands.add_parser(command, add_help=False)
        terminal.add_argument("--project", required=True)
        terminal.add_argument("--run-token", required=True)
    return parser


def _absolute_private_path(raw_path: str, label: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{label}_not_absolute")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError(f"{label}_parent_invalid")
    return path


def _read_json_object(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError:
        raise ValueError(f"{label}_unreadable") from None
    if len(raw) > max_bytes:
        raise ValueError(f"{label}_too_large")
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


class _RecoverableAtomicWrite:
    def __init__(self, path: Path, data: bytes) -> None:
        self._path = path
        self._data = data
        self._snapshot: tuple[bool, bytes] | None = None
        self._rolled_back = False

    def apply(self) -> None:
        if self._snapshot is not None:
            raise ValueError("private_file_write_failed")
        try:
            path_stat = self._path.lstat()
        except FileNotFoundError:
            self._snapshot = (False, b"")
        except OSError:
            raise ValueError("private_file_write_failed") from None
        else:
            original = self._read_original(path_stat)
            self._snapshot = (True, original)
        _atomic_write(self._path, self._data)

    def _read_original(self, path_stat: os.stat_result) -> bytes:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
        if not stat.S_ISREG(path_stat.st_mode) or (
            getattr(path_stat, "st_file_attributes", 0) & reparse_flag
        ):
            raise ValueError("private_file_write_failed")
        try:
            with self._path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or getattr(opened_stat, "st_file_attributes", 0)
                    & reparse_flag
                    or not os.path.samestat(path_stat, opened_stat)
                    or opened_stat.st_size > MAX_PRIVATE_INPUT_BYTES
                ):
                    raise ValueError("private_file_write_failed")
                original = stream.read(MAX_PRIVATE_INPUT_BYTES + 1)
            if (
                len(original) > MAX_PRIVATE_INPUT_BYTES
                or not os.path.samestat(path_stat, self._path.lstat())
            ):
                raise ValueError("private_file_write_failed")
        except ValueError:
            raise
        except OSError:
            raise ValueError("private_file_write_failed") from None
        return original

    def rollback(self) -> None:
        if self._rolled_back or self._snapshot is None:
            return
        existed, original = self._snapshot
        try:
            if existed:
                _atomic_write(self._path, original)
            else:
                self._path.unlink(missing_ok=True)
        except BaseException:
            raise ValueError("private_file_write_failed") from None
        self._rolled_back = True


def _selected_project(manifest: DwsManifest, project_id: str) -> DwsProjectManifest:
    matches = [item for item in manifest.projects if item.project_id == project_id]
    if len(matches) != 1:
        raise ValueError("project_not_found")
    return matches[0]


def _bundle_hash_payload(source_bundle: DwsSourceBundle) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": source_bundle.schema_version,
        "project_id": source_bundle.project_id,
        "project_name": source_bundle.project_name,
        "permission_scope": source_bundle.permission_scope,
        "collected_at": source_bundle.collected_at.isoformat(),
        "records": [
            record.model_dump(mode="json") for record in source_bundle.records
        ],
    }
    if source_bundle.retrieval_requests:
        payload["retrieval_requests"] = [
            request.model_dump(mode="json")
            for request in source_bundle.retrieval_requests
        ]
    return payload


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
    payload = _bundle_hash_payload(source_bundle)
    if _sha256(_canonical_bytes(payload).decode("utf-8")) != source_bundle.content_hash:
        raise ValueError("source_bundle_hash_mismatch")


def _normalized_text(value: str) -> str:
    return "".join(value.split()).casefold()


def _require_sourced_context(context: ProjectContextPackage) -> None:
    if (
        context.open_actions
        or context.current_risks
        or context.next_meeting is not None
    ):
        raise ValueError("context_fact_unreferenced")


def _all_references(context: ProjectContextPackage) -> tuple[EvidenceRef, ...]:
    nested = tuple(
        source
        for decision in context.active_decisions
        for source in decision.source_refs
    )
    sourced_facts = (*context.sourced_actions, *context.sourced_risks)
    if context.sourced_next_meeting is not None:
        sourced_facts += (context.sourced_next_meeting,)
    sourced = tuple(
        source
        for fact in sourced_facts
        for source in fact.source_refs
    )
    return (*context.source_refs, *nested, *sourced)


def _validate_context(
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
    context: ProjectContextPackage,
) -> None:
    _require_sourced_context(context)
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


def _completion_claims_hash(claims: tuple[RetrievalCompletionClaim, ...]) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            sorted(
                (claim.model_dump(mode="json") for claim in claims),
                key=lambda item: item["request_id"],
            )
        )
    ).hexdigest()


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
    available_request_ids = {
        item.request_id for item in source_bundle.retrieval_requests
    }
    if not set(completed_retrieval_request_ids).issubset(
        available_request_ids
    ):
        raise ValueError("retrieval_request_invalid")
    requests_by_id = {
        item.request_id: item for item in source_bundle.retrieval_requests
    }
    completed_claims = tuple(
        RetrievalCompletionClaim(
            request_id=request_id,
            request_epoch=requests_by_id[request_id].request_epoch,
            attempt_count=requests_by_id[request_id].attempt_count,
            lease_token=requests_by_id[request_id].lease_token,
        )
        for request_id in completed_retrieval_request_ids
    )
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
        completed_retrieval_request_ids=(),
        completed_retrieval_claims=completed_claims,
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
    payload = _read_json_object(
        path,
        "state_file",
        max_bytes=MAX_STATE_BYTES,
    )
    try:
        state = SyncCliState.model_validate(payload)
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


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _build_direct_opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}), _RejectRedirects())


def _direct_urlopen(request: Request, *, timeout: float) -> object:
    return _build_direct_opener().open(request, timeout=timeout)


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
    response_sync_id = payload["sync_id"]
    if not isinstance(response_sync_id, str):
        raise ValueError("response_invalid")
    if response_sync_id != envelope.sync_id:
        raise ValueError("response_sync_mismatch")
    outcome = payload["outcome"]
    if not isinstance(outcome, str) or outcome not in {
        "applied",
        "unchanged",
        "degraded",
    }:
        raise ValueError("response_invalid")
    project_status = payload["project_status"]
    if not isinstance(project_status, str) or project_status not in {
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
        not isinstance(generation_id, str)
        or not generation_id.strip()
        or len(generation_id) > 128
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


_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def _set_response_timeout(response: object, timeout: float) -> None:
    candidates = [response]
    for _depth in range(4):
        next_candidates: list[object] = []
        for candidate in candidates:
            settimeout = getattr(candidate, "settimeout", None)
            if callable(settimeout):
                settimeout(timeout)
                return
            for attribute in ("fp", "raw", "_sock"):
                child = getattr(candidate, attribute, None)
                if child is not None:
                    next_candidates.append(child)
        candidates = next_candidates


def _read_gateway_response(
    response: object,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> bytes:
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        read = getattr(response, "read", None)
        if not callable(read):
            raise ValueError("response_invalid")
        raw = read(65_537)
        if not isinstance(raw, bytes):
            raise ValueError("response_invalid")
        if monotonic() > deadline:
            raise ValueError("network_timeout")
        return raw

    chunks: list[bytes] = []
    total = 0
    while total <= 65_536:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError("network_timeout")
        _set_response_timeout(response, remaining)
        chunk = read1(min(8192, 65_537 - total))
        if not isinstance(chunk, bytes):
            raise ValueError("response_invalid")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if monotonic() > deadline:
        raise ValueError("network_timeout")
    return b"".join(chunks)


def _gateway_request(
    request: Request,
    *,
    opener: Callable[..., object],
    parse: Callable[[bytes], object],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> object:
    deadline = monotonic() + 30.0
    for attempt in range(3):
        remaining = 30.0 if attempt == 0 else deadline - monotonic()
        if remaining <= 0:
            raise ValueError("network_timeout")
        response = None
        retryable = False
        try:
            response = opener(request, timeout=remaining)
            raw = _read_gateway_response(
                response, deadline=deadline, monotonic=monotonic
            )
            return parse(raw)
        except HTTPError as exc:
            response = exc
            retryable = exc.code in _RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == 2:
                raise ValueError("http_error") from None
        except (TimeoutError, URLError, OSError):
            retryable = True
            if attempt == 2:
                raise ValueError("network_error") from None
        except ValueError:
            raise
        except Exception:
            raise ValueError("network_error") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if retryable:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ValueError("network_timeout")
            sleep(min(0.1 * (2**attempt), remaining))
    raise AssertionError("unreachable")


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
    if args.run_token:
        lifecycle.assert_stage(
            project.project_id,
            args.run_token,
            expected="begun",
            root=LIFECYCLE_ROOT,
            now=now,
        )
    else:
        lifecycle.assert_manual_allowed(
            project.project_id, root=LIFECYCLE_ROOT, now=now
        )
    actual_runner = runner
    if actual_runner is None:
        actual_runner = DwsCommandRunner(dws_path, profile=project.profile)
    source_bundle = collect_sources(project, actual_runner, clock=now)
    encoded = _canonical_bytes(source_bundle.model_dump(mode="json"))
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("sources_file_too_large")
    guard = (
        lifecycle.stage_guard(
            project.project_id,
            args.run_token,
            expected="begun",
            target="collected",
            root=LIFECYCLE_ROOT,
            now=now,
        )
        if args.run_token
        else lifecycle.manual_guard(
            project.project_id, root=LIFECYCLE_ROOT, now=now
        )
    )
    with guard:
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


def _host_import_command(
    args: argparse.Namespace,
    *,
    input_stream: object,
    now: Callable[[], datetime],
) -> dict[str, object]:
    lifecycle.assert_stage(
        args.project,
        args.run_token,
        expected="begun",
        root=LIFECYCLE_ROOT,
        now=now,
    )
    try:
        manifest_path = _absolute_private_path(args.manifest, "manifest")
        output_path = _absolute_private_path(args.output, "output")
        manifest = DwsManifest.load(manifest_path)
        project = _selected_project(manifest, args.project)
    except Exception:
        raise ValueError("host_import_invalid") from None
    read = getattr(input_stream, "read", None)
    if not callable(read):
        raise ValueError("host_import_invalid")
    try:
        raw = read(MAX_HOST_IMPORT_BYTES + 1)
    except Exception:
        raise ValueError("host_import_invalid") from None
    if not isinstance(raw, bytes) or len(raw) > MAX_HOST_IMPORT_BYTES:
        raise ValueError("host_import_invalid")
    collected_at = now()
    try:
        source_bundle = import_single_document_bundle(
            raw,
            project,
            collected_at=collected_at,
        )
        encoded = _canonical_bytes(source_bundle.model_dump(mode="json"))
    except Exception:
        raise ValueError("host_import_invalid") from None
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("host_import_invalid")
    output_transaction = _RecoverableAtomicWrite(output_path, encoded)
    lifecycle.commit_stage(
        project.project_id,
        args.run_token,
        expected="begun",
        target="collected",
        apply=output_transaction.apply,
        rollback=output_transaction.rollback,
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {
        "status": "collected",
        "project_id": project.project_id,
        "source_count": 1,
        "active_sources": 1,
        "failed_sources": 0,
        "content_hash": source_bundle.content_hash,
        "output_bytes": len(encoded),
    }


def _pending_response(
    raw: bytes, project_id: str
) -> tuple[ClaimedRetrievalRequest, ...]:
    if len(raw) > 65_536:
        raise ValueError("response_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError("response_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"requests"}:
        raise ValueError("response_invalid")
    raw_requests = payload["requests"]
    if not isinstance(raw_requests, list) or len(raw_requests) > 100:
        raise ValueError("response_invalid")
    requests: list[ClaimedRetrievalRequest] = []
    try:
        for value in raw_requests:
            request = ClaimedRetrievalRequest.model_validate(value)
            if (
                request.project_id != project_id
                or request.status is not RetrievalRequestStatus.IN_PROGRESS
                or request.baseline_generation_id is None
            ):
                raise ValueError("invalid pending request")
            requests.append(request)
    except (TypeError, ValueError):
        raise ValueError("response_invalid") from None
    if len({item.request_id for item in requests}) != len(requests):
        raise ValueError("response_invalid")
    return tuple(requests)


def _pending_command_inner(
    args: argparse.Namespace,
    *,
    opener: Callable[..., object],
    environ: Mapping[str, str],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> dict[str, object]:
    gateway = _gateway_base(args.gateway)
    manifest_path = _absolute_private_path(args.manifest, "manifest")
    sources_path = _absolute_private_path(args.sources_file, "sources_file")
    manifest = DwsManifest.load(manifest_path)
    project = _selected_project(manifest, args.project)
    try:
        source_bundle = DwsSourceBundle.model_validate(
            _read_json_object(
                sources_path,
                "sources_file",
                max_bytes=MAX_PRIVATE_INPUT_BYTES,
            )
        )
    except (TypeError, ValueError):
        raise ValueError("sources_file_invalid") from None
    _validate_bundle(project, source_bundle)
    token = environ.get(TOKEN_ENVIRONMENT_VARIABLE, "")
    if not token:
        raise ValueError("token_missing")
    if "\r" in token or "\n" in token:
        raise ValueError("token_invalid")
    request = Request(
        (
            f"{gateway}/v1/projects/"
            f"{quote(project.project_id, safe='')}/retrieval-requests"
            "?status=pending"
        ),
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    pending = _gateway_request(
        request,
        opener=opener,
        parse=lambda raw: _pending_response(raw, project.project_id),
        monotonic=monotonic,
        sleep=sleep,
    )
    assert isinstance(pending, tuple)
    sources_by_hash: dict[str, DwsRetrievalSource] = {}
    for source in project.sources:
        source_hash = _sha256(source.source_id)
        if source_hash in sources_by_hash:
            raise ValueError("retrieval_request_invalid")
        sources_by_hash[source_hash] = DwsRetrievalSource(
            source_type=source.source_type,
            source_id=source.source_id,
        )
    mapped: list[DwsRetrievalRequest] = []
    try:
        for item in pending:
            request_sources = tuple(
                sources_by_hash[source_hash]
                for source_hash in item.source_id_hashes
            )
            mapped.append(
                DwsRetrievalRequest(
                    request_id=item.request_id,
                    query_hash=item.query_hash,
                    request_epoch=item.request_epoch,
                    attempt_count=item.attempt_count,
                    lease_expires_at=item.lease_expires_at,
                    lease_token=item.lease_token,
                    sources=request_sources,
                )
            )
    except (KeyError, TypeError, ValueError):
        raise ValueError("retrieval_request_invalid") from None
    payload = _bundle_hash_payload(source_bundle)
    if mapped:
        payload["retrieval_requests"] = [
            item.model_dump(mode="json") for item in mapped
        ]
    else:
        payload.pop("retrieval_requests", None)
    updated = DwsSourceBundle(
        **payload,
        content_hash=_sha256(_canonical_bytes(payload).decode("utf-8")),
    )
    encoded = _canonical_bytes(updated.model_dump(mode="json"))
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("sources_file_too_large")
    _atomic_write(sources_path, encoded)
    requested_sources = {
        (source.source_type, source.source_id)
        for item in mapped
        for source in item.sources
    }
    return {
        "status": "pending_fetched",
        "project_id": project.project_id,
        "request_count": len(mapped),
        "source_count": len(requested_sources),
        "content_hash": updated.content_hash,
    }


def _pending_command(
    args: argparse.Namespace,
    *,
    opener: Callable[..., object],
    environ: Mapping[str, str],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> dict[str, object]:
    manifest = DwsManifest.load(
        _absolute_private_path(args.manifest, "manifest")
    )
    project = _selected_project(manifest, args.project)
    guard = (
        lifecycle.stage_guard(
            project.project_id,
            args.run_token,
            expected="collected",
            target="pending",
            root=LIFECYCLE_ROOT,
            now=now,
        )
        if args.run_token
        else lifecycle.manual_guard(
            project.project_id, root=LIFECYCLE_ROOT, now=now
        )
    )
    with guard:
        return _pending_command_inner(
            args,
            opener=opener,
            environ=environ,
            monotonic=monotonic,
            sleep=sleep,
            now=now,
        )


def _artifact_command(
    args: argparse.Namespace,
    *,
    input_stream: object,
    now: Callable[[], datetime],
) -> dict[str, object]:
    project_id = _safe_id(args.project, "project_id")
    context_path = _absolute_private_path(args.context_file, "context_file")
    read = getattr(input_stream, "read", None)
    if not callable(read):
        raise ValueError("context_file_invalid")
    raw = read(MAX_PRIVATE_INPUT_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("context_file_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
        artifact = QwenProjectContextArtifact.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        raise ValueError("context_file_invalid") from None
    if artifact.context.project_id != project_id:
        raise ValueError("context_mismatch")
    encoded = _canonical_bytes(artifact.model_dump(mode="json"))
    with lifecycle.stage_guard(
        project_id,
        args.run_token,
        expected="pending",
        target="artifact",
        root=LIFECYCLE_ROOT,
        now=now,
    ):
        _atomic_write(context_path, encoded)
    return {
        "status": "artifact_written",
        "project_id": project_id,
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
    sources_payload = _read_json_object(
        sources_path,
        "sources_file",
        max_bytes=MAX_PRIVATE_INPUT_BYTES,
    )
    try:
        source_bundle = DwsSourceBundle.model_validate(sources_payload)
    except (TypeError, ValueError):
        raise ValueError("sources_file_invalid") from None
    context_payload = _read_json_object(
        context_path,
        "context_file",
        max_bytes=MAX_PRIVATE_INPUT_BYTES,
    )
    try:
        artifact = QwenProjectContextArtifact.model_validate(context_payload)
    except ValidationError as exc:
        if "context_fact_unreferenced" in str(exc):
            raise ValueError("context_fact_unreferenced") from None
        raise ValueError("context_file_invalid") from None
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
    sleep: Callable[[float], None],
) -> dict[str, object]:
    gateway = _gateway_base(args.gateway)
    project, source_bundle, artifact, state_path = _read_push_inputs(args)

    if args.dry_run:
        if args.run_token:
            lifecycle.assert_stage(
                project.project_id,
                args.run_token,
                expected="artifact",
                root=LIFECYCLE_ROOT,
                now=now,
            )
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
    guard = (
        lifecycle.stage_guard(
            project.project_id,
            args.run_token,
            expected="artifact",
            target="pushed",
            root=LIFECYCLE_ROOT,
            now=now,
        )
        if args.run_token
        else lifecycle.manual_guard(
            project.project_id,
            root=LIFECYCLE_ROOT,
            now=now,
            timeout=SYNC_LOCK_TIMEOUT_SECONDS,
        )
    )
    with guard:
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
            completed_retrieval_request_ids=(
                artifact.completed_retrieval_request_ids
            ),
            source_cursor=cursor,
            now=now(),
        )
        if state.pending is not None:
            if state.pending.content_hash != envelope.content_hash:
                raise ValueError("pending_sync_conflict")
            if state.pending.completion_claims_hash != _completion_claims_hash(
                envelope.completed_retrieval_claims
            ):
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
            completion_claims_hash=_completion_claims_hash(
                envelope.completed_retrieval_claims
            ),
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
        response_payload = _gateway_request(
            request,
            opener=opener,
            parse=lambda raw: _validate_response(raw, envelope=envelope),
            monotonic=monotonic,
            sleep=sleep,
        )
        assert isinstance(response_payload, dict)
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
    urlopen: Callable[..., object] = _direct_urlopen,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    input_stream: object | None = None,
) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in actual_argv or "-h" in actual_argv:
        if actual_argv and actual_argv[0] in {
            "abort",
            "artifact",
            "begin",
            "collect",
            "host-import",
            "end",
            "pending",
            "push",
        }:
            output: dict[str, object] = {
                "status": "help",
                "command": actual_argv[0],
            }
        else:
            output = {
                "status": "help",
                "commands": [
                    "begin",
                    "collect",
                    "host-import",
                    "pending",
                    "artifact",
                    "push",
                    "end",
                    "abort",
                ],
            }
        _emit(output)
        return 0
    try:
        args = _parser().parse_args(actual_argv)
        if args.command == "begin":
            started = lifecycle.begin_run(
                args.project, root=LIFECYCLE_ROOT, now=now
            )
            output = {
                "status": started.status,
                "project_id": args.project,
                "run_token": started.run_token,
            }
        elif args.command == "collect":
            output = _collect_command(args, runner=runner, now=now)
        elif args.command == "host-import":
            output = _host_import_command(
                args,
                input_stream=(
                    sys.stdin.buffer if input_stream is None else input_stream
                ),
                now=now,
            )
        elif args.command == "pending":
            output = _pending_command(
                args,
                opener=urlopen,
                environ=os.environ if environ is None else environ,
                monotonic=monotonic,
                sleep=sleep,
                now=now,
            )
        elif args.command == "artifact":
            output = _artifact_command(
                args,
                input_stream=(
                    sys.stdin.buffer if input_stream is None else input_stream
                ),
                now=now,
            )
        elif args.command == "end":
            ended = lifecycle.end_run(
                args.project,
                args.run_token,
                root=LIFECYCLE_ROOT,
                now=now,
            )
            output = {
                "status": ended.status,
                "project_id": args.project,
                "run_token": ended.run_token,
            }
        elif args.command == "abort":
            lifecycle.abort_run(
                args.project,
                args.run_token,
                root=LIFECYCLE_ROOT,
                now=now,
            )
            output = {"status": "aborted", "project_id": args.project}
        else:
            output = _push_command(
                args,
                opener=urlopen,
                environ=os.environ if environ is None else environ,
                now=now,
                monotonic=monotonic,
                sleep=sleep,
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
