from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
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

from companion_gateway.project.evidence_validation import validate_sourced_context
from companion_gateway.project.index import chunk_text
from companion_gateway.project.models import ProjectContextPackage
from companion_gateway.project.protection import ContentProtector, WindowsDpapiProtector
from companion_gateway.project.sync_models import (
    ClaimedRetrievalRequest,
    RetrievalCompletionClaim,
    RetrievalRequestStatus,
    SourceErrorType,
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
    from tools.dws_sync import host_capture
    from tools.dws_sync.runtime import approved_artifact_path
    from tools.dws_sync.host_bridge import (
        MAX_HOST_IMPORT_BYTES,
        build_single_document_bundle,
        decode_host_result,
        decode_host_results,
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
    from dws_sync import host_capture  # type: ignore[no-redef]
    from dws_sync.runtime import approved_artifact_path  # type: ignore[no-redef]
    from dws_sync.host_bridge import (  # type: ignore[no-redef]
        MAX_HOST_IMPORT_BYTES,
        build_single_document_bundle,
        decode_host_result,
        decode_host_results,
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
_SAFE_GATEWAY_ERROR_TYPES = frozenset(
    {
        "clock_skew_exceeded",
        "completion_claims_conflict",
        "content_hash_mismatch",
        "context_conflict",
        "context_fact_unreferenced",
        "cursor_content_conflict",
        "invalid_envelope",
        "now_must_be_aware",
        "project_access_denied",
        "project_api_authentication_failed",
        "project_api_authentication_required",
        "project_api_disabled",
        "project_review_denied",
        "project_scope_denied",
        "retrieval_claim_expired",
        "retrieval_claim_invalid",
        "retrieval_claim_required",
        "retrieval_evidence_missing",
        "retrieval_request_conflict",
        "source_excerpt_mismatch",
        "source_ref_mismatch",
        "stale_cursor",
        "sync_body_too_large",
        "sync_conflict",
        "sync_host_forbidden",
        "sync_internal_error",
        "sync_invalid_content_length",
        "sync_invalid_request",
        "sync_project_mismatch",
        "sync_proxy_headers_forbidden",
    }
)
_PUBLIC_ERROR_TYPES = {
    "arguments_invalid",
    "approved_artifact_unavailable",
    "approved_restore_denied",
    "authentication_failed",
    "dws_core_changed_requires_approval",
    "context_collection_mismatch",
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
    "host_capture_conflict",
    "host_capture_invalid",
    "host_capture_missing",
    "host_capture_remaining",
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
    "pending_discard_denied",
    "pending_sync_conflict",
    "pending_recovery_denied",
    "permission_denied",
    "private_paths_overlap",
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
    "database_file_not_absolute",
    "database_file_parent_invalid",
    "database_file_not_regular_file",
    "sync_lock_timeout",
    "sync_failed",
    "token_invalid",
    "token_missing",
    "lifecycle_state_invalid",
    "lifecycle_active",
    "run_stage_invalid",
    "run_token_invalid",
    "unknown",
} | _SAFE_GATEWAY_ERROR_TYPES


def _host_capture_protector() -> ContentProtector:
    return WindowsDpapiProtector()


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
    context_semantic_hash: str | None = None
    failure_type: Literal["sync_conflict"] | None = None

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

    @field_validator("context_semantic_hash")
    @classmethod
    def validate_context_semantic_hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("context_semantic_hash_invalid")
        return value


def _pending_identity_matches(
    pending: PendingSync,
    envelope: SyncEnvelope,
) -> bool:
    return (
        pending.content_hash == envelope.content_hash
        and pending.sync_id == envelope.sync_id
        and pending.completion_claims_hash
        == _completion_claims_hash(envelope.completed_retrieval_claims)
    )


def _context_semantic_hash(context: ProjectContextPackage) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            context.model_dump(mode="json", exclude={"generated_at"})
        )
    ).hexdigest()


def _pending_rebuild_allowed(
    pending: PendingSync,
    envelope: SyncEnvelope,
) -> bool:
    return (
        pending.failure_type == "sync_conflict"
        and pending.context_semantic_hash is not None
        and hmac.compare_digest(
            pending.context_semantic_hash,
            _context_semantic_hash(envelope.context),
        )
        and hmac.compare_digest(
            pending.completion_claims_hash,
            _completion_claims_hash(envelope.completed_retrieval_claims),
        )
    )


class SyncCliState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    project_id: str
    last_cursor: int = Field(ge=0)
    last_content_hash: str | None
    last_sync_id: str | None
    pending: PendingSync | None
    last_source_semantic_hash: str | None = None
    last_artifact_hash: str | None = None

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

    @field_validator("last_source_semantic_hash", "last_artifact_hash")
    @classmethod
    def validate_artifact_hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("state_artifact_hashes_invalid")
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
        semantic_pair = (
            self.last_source_semantic_hash,
            self.last_artifact_hash,
        )
        if (semantic_pair[0] is None) != (semantic_pair[1] is None) or (
            self.last_cursor == 0 and semantic_pair != (None, None)
        ):
            raise ValueError("state_artifact_hashes_invalid")
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

    capture_info = commands.add_parser("capture-info", add_help=False)
    capture_info.add_argument("--manifest", required=True)
    capture_info.add_argument("--project", required=True)
    capture_info.add_argument("--output")
    capture_info.add_argument("--run-token", required=True)

    complete_host_import = commands.add_parser(
        "complete-host-import", add_help=False
    )
    complete_host_import.add_argument("--manifest", required=True)
    complete_host_import.add_argument("--project", required=True)
    complete_host_import.add_argument("--output", required=True)
    complete_host_import.add_argument("--run-token", required=True)

    pending = commands.add_parser("pending", add_help=False)
    pending.add_argument("--manifest", required=True)
    pending.add_argument("--project", required=True)
    pending.add_argument("--sources-file", required=True)
    pending.add_argument("--gateway", required=True)
    pending.add_argument("--run-token")

    artifact = commands.add_parser("artifact", add_help=False)
    artifact.add_argument("--manifest", required=True)
    artifact.add_argument("--project", required=True)
    artifact.add_argument("--sources-file", required=True)
    artifact.add_argument("--context-file", required=True)
    artifact.add_argument("--state-file", required=True)
    artifact.add_argument("--run-token", required=True)

    reuse_artifact = commands.add_parser("reuse-artifact", add_help=False)
    reuse_artifact.add_argument("--manifest", required=True)
    reuse_artifact.add_argument("--project", required=True)
    reuse_artifact.add_argument("--sources-file", required=True)
    reuse_artifact.add_argument("--context-file", required=True)
    reuse_artifact.add_argument("--state-file", required=True)
    reuse_artifact.add_argument("--run-token", required=True)
    reuse_artifact.add_argument("--unattended", action="store_true")

    restore_approved = commands.add_parser("restore-approved", add_help=False)
    restore_approved.add_argument("--manifest", required=True)
    restore_approved.add_argument("--project", required=True)
    restore_approved.add_argument("--sources-file", required=True)
    restore_approved.add_argument("--context-file", required=True)
    restore_approved.add_argument("--state-file", required=True)
    restore_approved.add_argument("--run-token", required=True)

    push = commands.add_parser("push", add_help=False)
    push.add_argument("--manifest", required=True)
    push.add_argument("--project", required=True)
    push.add_argument("--sources-file", required=True)
    push.add_argument("--context-file", required=True)
    push.add_argument("--state-file", required=True)
    push.add_argument("--gateway", required=True)
    push.add_argument("--dry-run", action="store_true")
    push.add_argument("--run-token")

    recover_pending = commands.add_parser("recover-pending", add_help=False)
    recover_pending.add_argument("--manifest", required=True)
    recover_pending.add_argument("--project", required=True)
    recover_pending.add_argument("--sources-file", required=True)
    recover_pending.add_argument("--context-file", required=True)
    recover_pending.add_argument("--state-file", required=True)
    recover_pending.add_argument("--database-file", required=True)

    discard_pending = commands.add_parser(
        "discard-rejected-pending", add_help=False
    )
    discard_pending.add_argument("--manifest", required=True)
    discard_pending.add_argument("--project", required=True)
    discard_pending.add_argument("--state-file", required=True)
    discard_pending.add_argument("--database-file", required=True)
    discard_pending.add_argument("--confirm", required=True)

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


def _validate_distinct_private_paths(paths: tuple[Path, ...]) -> None:
    normalized = [
        os.path.normcase(os.path.normpath(str(path))) for path in paths
    ]
    if len(set(normalized)) != len(paths):
        raise ValueError("private_paths_overlap")
    try:
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path.exists() and other.exists() and path.samefile(other):
                    raise ValueError("private_paths_overlap")
    except ValueError:
        raise
    except OSError:
        raise ValueError("private_paths_overlap") from None


def _validated_sync_paths(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Path]:
    manifest_path = _absolute_private_path(args.manifest, "manifest")
    sources_path = _absolute_private_path(args.sources_file, "sources_file")
    context_path = _absolute_private_path(args.context_file, "context_file")
    state_path = _absolute_private_path(args.state_file, "state_file")
    _validate_distinct_private_paths(
        (
            manifest_path,
            sources_path,
            context_path,
            approved_artifact_path(context_path),
            state_path,
        )
    )
    return manifest_path, sources_path, context_path, state_path


def _safe_regular_file_stat(info: os.stat_result, *, max_bytes: int) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & reparse_flag
        and info.st_nlink == 1
        and info.st_size <= max_bytes
    )


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


def _read_restore_json_object(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> dict[str, object]:
    try:
        path_stat = path.lstat()
        if not _safe_regular_file_stat(path_stat, max_bytes=max_bytes):
            raise ValueError(f"{label}_invalid")
        with path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if (
                not _safe_regular_file_stat(opened_stat, max_bytes=max_bytes)
                or not os.path.samestat(path_stat, opened_stat)
            ):
                raise ValueError(f"{label}_invalid")
            raw = stream.read(max_bytes + 1)
            final_opened_stat = os.fstat(stream.fileno())
        final_path_stat = path.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError(f"{label}_unreadable") from None
    if (
        len(raw) > max_bytes
        or not _safe_regular_file_stat(final_opened_stat, max_bytes=max_bytes)
        or not _safe_regular_file_stat(final_path_stat, max_bytes=max_bytes)
        or not os.path.samestat(path_stat, final_opened_stat)
        or not os.path.samestat(path_stat, final_path_stat)
    ):
        raise ValueError(f"{label}_invalid")
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
        if not _safe_regular_file_stat(
            path_stat,
            max_bytes=MAX_PRIVATE_INPUT_BYTES,
        ):
            raise ValueError("private_file_write_failed")
        try:
            with self._path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                if (
                    not _safe_regular_file_stat(
                        opened_stat,
                        max_bytes=MAX_PRIVATE_INPUT_BYTES,
                    )
                    or not os.path.samestat(path_stat, opened_stat)
                ):
                    raise ValueError("private_file_write_failed")
                original = stream.read(MAX_PRIVATE_INPUT_BYTES + 1)
                final_opened_stat = os.fstat(stream.fileno())
            final_path_stat = self._path.lstat()
            if (
                len(original) > MAX_PRIVATE_INPUT_BYTES
                or not _safe_regular_file_stat(
                    final_opened_stat,
                    max_bytes=MAX_PRIVATE_INPUT_BYTES,
                )
                or not _safe_regular_file_stat(
                    final_path_stat,
                    max_bytes=MAX_PRIVATE_INPUT_BYTES,
                )
                or not os.path.samestat(path_stat, final_opened_stat)
                or not os.path.samestat(path_stat, final_path_stat)
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


def source_bundle_semantic_hash(bundle: DwsSourceBundle) -> str:
    fields = (
        "source_type",
        "source_id",
        "permission_scope",
        "status",
        "source_title",
        "source_url",
        "source_version",
        "source_time",
        "content_hash",
        "error_type",
        "retryable",
    )
    records = sorted(
        bundle.records,
        key=lambda item: (item.source_type.value, item.source_id),
    )
    payload = {
        "project_id": bundle.project_id,
        "project_name": bundle.project_name,
        "permission_scope": bundle.permission_scope,
        "records": [
            {field: item.model_dump(mode="json")[field] for field in fields}
            for item in records
        ],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _read_approved_artifact(
    context_path: Path,
    expected_hash: str,
) -> QwenProjectContextArtifact:
    path = approved_artifact_path(context_path)
    try:
        path_stat = path.lstat()
        if not _safe_regular_file_stat(
            path_stat,
            max_bytes=MAX_PRIVATE_INPUT_BYTES,
        ):
            raise ValueError("approved_artifact_unavailable")
        with path.open("rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if (
                not _safe_regular_file_stat(
                    opened_stat,
                    max_bytes=MAX_PRIVATE_INPUT_BYTES,
                )
                or not os.path.samestat(path_stat, opened_stat)
            ):
                raise ValueError("approved_artifact_unavailable")
            raw = stream.read(MAX_PRIVATE_INPUT_BYTES + 1)
            final_opened_stat = os.fstat(stream.fileno())
        final_path_stat = path.lstat()
        if (
            len(raw) > MAX_PRIVATE_INPUT_BYTES
            or not _safe_regular_file_stat(
                final_opened_stat,
                max_bytes=MAX_PRIVATE_INPUT_BYTES,
            )
            or not _safe_regular_file_stat(
                final_path_stat,
                max_bytes=MAX_PRIVATE_INPUT_BYTES,
            )
            or not os.path.samestat(path_stat, final_opened_stat)
            or not os.path.samestat(path_stat, final_path_stat)
        ):
            raise ValueError("approved_artifact_unavailable")
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_non_finite,
        )
        artifact = QwenProjectContextArtifact.model_validate(payload)
        actual_hash = hashlib.sha256(
            _canonical_bytes(artifact.model_dump(mode="json"))
        ).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise ValueError("approved_artifact_unavailable")
        return artifact
    except ValueError as exc:
        if str(exc) == "approved_artifact_unavailable":
            raise
        raise ValueError("approved_artifact_unavailable") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise ValueError("approved_artifact_unavailable") from None


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


def _require_sourced_context(context: ProjectContextPackage) -> None:
    if (
        context.open_actions
        or context.current_risks
        or context.next_meeting is not None
    ):
        raise ValueError("context_fact_unreferenced")


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
    if context.generated_at != source_bundle.collected_at:
        raise ValueError("context_collection_mismatch")
    snapshots = (
        _source_snapshot(record)
        for record in source_bundle.records
        if record.status in {"active", "failed"}
    )
    validate_sourced_context(context, snapshots)


def _validate_restore_context(
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
    if context.generated_at > source_bundle.collected_at:
        raise ValueError("approved_restore_denied")
    active_records = (
        record for record in source_bundle.records if record.status == "active"
    )
    if any(
        record.source_time is not None
        and record.source_time > context.generated_at
        for record in active_records
    ):
        raise ValueError("approved_restore_denied")
    snapshots = (
        _source_snapshot(record)
        for record in source_bundle.records
        if record.status in {"active", "failed"}
    )
    validate_sourced_context(context, snapshots)


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
        generated_at=now,
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
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError("network_timeout")
        _set_response_timeout(response, remaining)
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


def _safe_gateway_error_type(
    error: HTTPError,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> str:
    try:
        raw = _read_gateway_response(
            error,
            deadline=deadline,
            monotonic=monotonic,
        )
    except (OSError, TimeoutError, ValueError):
        return "http_error"
    if len(raw) > MAX_STATE_BYTES:
        return "http_error"
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_non_finite,
            object_pairs_hook=tuple,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return "http_error"
    if (
        not isinstance(payload, tuple)
        or len(payload) != 1
        or payload[0][0] != "detail"
    ):
        return "http_error"
    detail = payload[0][1]
    if not isinstance(detail, str) or detail not in _SAFE_GATEWAY_ERROR_TYPES:
        return "http_error"
    return detail


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
            if not retryable:
                raise ValueError(
                    _safe_gateway_error_type(
                        exc,
                        deadline=deadline,
                        monotonic=monotonic,
                    )
                ) from None
            if attempt == 2:
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
    direct_collection: bool = False,
) -> dict[str, object]:
    manifest_path = _absolute_private_path(args.manifest, "manifest")
    output_path = _absolute_private_path(args.output, "output")
    dws_path = Path(args.dws_path)
    if direct_collection and runner is None:
        raise ValueError("arguments_invalid")
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
    if direct_collection:
        failed_types = {
            record.error_type
            for record in source_bundle.records
            if record.status == "failed"
        }
        if failed_types:
            if len(failed_types) == 1:
                error_type = next(iter(failed_types))
                if (
                    isinstance(error_type, SourceErrorType)
                    and error_type is not SourceErrorType.UNKNOWN
                ):
                    raise ValueError(error_type.value)
            raise ValueError("sync_failed")
    encoded = _canonical_bytes(source_bundle.model_dump(mode="json"))
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("sources_file_too_large")
    if direct_collection:
        if not args.run_token:
            raise ValueError("arguments_invalid")
        transaction = _RecoverableAtomicWrite(output_path, encoded)
        lifecycle.commit_direct_collection(
            project.project_id,
            args.run_token,
            apply=transaction.apply,
            rollback=transaction.rollback,
            root=LIFECYCLE_ROOT,
            now=now,
        )
    else:
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


def _host_import_project(args: argparse.Namespace) -> DwsProjectManifest:
    try:
        manifest_path = _absolute_private_path(args.manifest, "manifest")
        manifest = DwsManifest.load(manifest_path)
        return _selected_project(manifest, args.project)
    except Exception:
        raise ValueError("host_import_invalid") from None


def _read_host_input(input_stream: object) -> bytes:
    read = getattr(input_stream, "read", None)
    if not callable(read):
        raise ValueError("host_import_invalid")
    try:
        raw = read(MAX_HOST_IMPORT_BYTES + 1)
    except Exception:
        raise ValueError("host_import_invalid") from None
    if not isinstance(raw, bytes) or len(raw) > MAX_HOST_IMPORT_BYTES:
        raise ValueError("host_import_invalid")
    return raw


def _host_capture_stage(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
    allow_existing: bool,
) -> Literal["begun", "host_info"]:
    try:
        lifecycle.assert_stage(
            args.project,
            args.run_token,
            expected="begun",
            root=LIFECYCLE_ROOT,
            now=now,
        )
        return "begun"
    except ValueError as exc:
        if not allow_existing or str(exc) != "run_stage_invalid":
            raise
    lifecycle.assert_stage(
        args.project,
        args.run_token,
        expected="host_info",
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return "host_info"


def _capture_document_info(
    project: DwsProjectManifest,
    run_token: str,
    document_info: dict[str, object],
    *,
    now: Callable[[], datetime],
) -> None:
    try:
        transaction = host_capture.prepare_document_info_capture(
            document_info,
            project,
            run_token=run_token,
            protector=_host_capture_protector(),
        )
    except Exception as exc:
        if str(exc) in {"host_capture_conflict", "host_capture_invalid"}:
            raise
        raise ValueError("host_capture_invalid") from None
    try:
        lifecycle.commit_stage(
            project.project_id,
            run_token,
            expected="begun",
            target="host_info",
            apply=transaction.apply,
            rollback=transaction.rollback,
            root=LIFECYCLE_ROOT,
            now=now,
        )
    except ValueError as exc:
        if str(exc) != "run_stage_invalid":
            raise
        lifecycle.apply_stage(
            project.project_id,
            run_token,
            expected="host_info",
            apply=transaction.apply,
            rollback=transaction.rollback,
            root=LIFECYCLE_ROOT,
            now=now,
        )


class _HostImportCommit:
    def __init__(
        self,
        output: _RecoverableAtomicWrite,
        capture: host_capture.DocumentInfoCaptureDelete,
    ) -> None:
        self._output = output
        self._capture = capture

    def apply(self) -> None:
        self._output.apply()
        try:
            self._capture.apply()
        except BaseException:
            try:
                self._output.rollback()
            except BaseException:
                raise ValueError("private_file_write_failed") from None
            raise

    def rollback(self) -> None:
        capture_error = False
        try:
            self._capture.rollback()
        except BaseException:
            capture_error = True
        try:
            self._output.rollback()
        except BaseException:
            raise ValueError("private_file_write_failed") from None
        if capture_error:
            raise ValueError("private_file_write_failed")


def _complete_host_import(
    args: argparse.Namespace,
    project: DwsProjectManifest,
    document_read: dict[str, object],
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    try:
        output_path = _absolute_private_path(args.output, "output")
        capture = host_capture.prepare_document_info_capture_delete(
            project,
            run_token=args.run_token,
            protector=_host_capture_protector(),
        )
        document_info = capture.load().document_info
        source_bundle = build_single_document_bundle(
            document_info,
            document_read,
            project,
            collected_at=now(),
        )
        encoded = _canonical_bytes(source_bundle.model_dump(mode="json"))
    except ValueError as exc:
        if str(exc) in {"host_capture_invalid", "host_capture_missing"}:
            raise
        raise ValueError("host_import_invalid") from None
    except Exception:
        raise ValueError("host_import_invalid") from None
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("host_import_invalid")
    transaction = _HostImportCommit(
        _RecoverableAtomicWrite(output_path, encoded),
        capture,
    )
    lifecycle.commit_stage(
        project.project_id,
        args.run_token,
        expected="host_info",
        target="collected",
        apply=transaction.apply,
        rollback=transaction.rollback,
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


def _capture_info_command(
    args: argparse.Namespace,
    *,
    input_stream: object,
    now: Callable[[], datetime],
) -> dict[str, object]:
    project = _host_import_project(args)
    try:
        document_info = decode_host_result(
            _read_host_input(input_stream),
            "doc_info",
        )
    except Exception:
        raise ValueError("host_import_invalid") from None
    _capture_document_info(
        project,
        args.run_token,
        document_info,
        now=now,
    )
    return {"status": "host_info_captured", "project_id": project.project_id}


def _complete_host_import_command(
    args: argparse.Namespace,
    *,
    input_stream: object,
    now: Callable[[], datetime],
) -> dict[str, object]:
    lifecycle.assert_stage(
        args.project,
        args.run_token,
        expected="host_info",
        root=LIFECYCLE_ROOT,
        now=now,
    )
    project = _host_import_project(args)
    try:
        document_read = decode_host_result(
            _read_host_input(input_stream),
            "doc_read",
        )
    except Exception:
        raise ValueError("host_import_invalid") from None
    return _complete_host_import(
        args,
        project,
        document_read,
        now=now,
    )


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
    project = _host_import_project(args)
    try:
        document_info, document_read = decode_host_results(
            _read_host_input(input_stream),
            project.project_id,
        )
    except Exception:
        raise ValueError("host_import_invalid") from None
    try:
        _capture_document_info(
            project,
            args.run_token,
            document_info,
            now=now,
        )
    except ValueError as exc:
        if str(exc) in {
            "host_capture_conflict",
            "host_capture_invalid",
            "host_capture_missing",
        }:
            raise ValueError("host_import_invalid") from None
        raise
    return _complete_host_import(
        args,
        project,
        document_read,
        now=now,
    )


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
    manifest_path, sources_path, context_path, state_path = (
        _validated_sync_paths(args)
    )
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
    _validate_bundle(project, source_bundle)
    read = getattr(input_stream, "read", None)
    if not callable(read):
        raise ValueError("context_file_invalid")
    raw = read(MAX_PRIVATE_INPUT_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("context_file_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
        artifact = QwenProjectContextArtifact.model_validate(payload)
    except ValidationError as exc:
        if "context_fact_unreferenced" in str(exc):
            raise ValueError("context_fact_unreferenced") from None
        raise ValueError("context_file_invalid") from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("context_file_invalid") from None
    selected_bytes: list[bytes] = []
    output_transactions: list[_RecoverableAtomicWrite] = []

    def apply() -> None:
        state = _load_state(state_path, project.project_id)
        semantic_hash = source_bundle_semantic_hash(source_bundle)
        selected = artifact
        if state.last_source_semantic_hash == semantic_hash:
            assert state.last_artifact_hash is not None
            approved = _read_approved_artifact(
                context_path,
                state.last_artifact_hash,
            )
            selected = QwenProjectContextArtifact(
                schema_version=1,
                context=approved.context.model_copy(
                    update={"generated_at": source_bundle.collected_at}
                ),
                completed_retrieval_request_ids=(
                    artifact.completed_retrieval_request_ids
                ),
            )
        _validate_context(project, source_bundle, selected.context)
        if state.pending is not None:
            pending_envelope = _build_envelope(
                project,
                source_bundle,
                selected.context,
                completed_retrieval_request_ids=(
                    selected.completed_retrieval_request_ids
                ),
                source_cursor=state.pending.source_cursor,
                now=now(),
            )
            if (
                not _pending_identity_matches(state.pending, pending_envelope)
                and not _pending_rebuild_allowed(
                    state.pending,
                    pending_envelope,
                )
            ):
                raise ValueError("pending_sync_conflict")
        encoded = _canonical_bytes(selected.model_dump(mode="json"))
        if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
            raise ValueError("context_file_too_large")
        transaction = _RecoverableAtomicWrite(context_path, encoded)
        output_transactions.append(transaction)
        selected_bytes.append(encoded)
        transaction.apply()

    def rollback() -> None:
        if output_transactions:
            output_transactions[0].rollback()

    lifecycle.commit_stage(
        project.project_id,
        args.run_token,
        expected="pending",
        target="artifact",
        apply=apply,
        rollback=rollback,
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {
        "status": "artifact_written",
        "project_id": project.project_id,
        "output_bytes": len(selected_bytes[0]),
    }


def _reuse_artifact_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    manifest_path, sources_path, context_path, state_path = (
        _validated_sync_paths(args)
    )
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
    _validate_bundle(project, source_bundle)
    lifecycle.assert_stage(
        project.project_id,
        args.run_token,
        expected="pending",
        root=LIFECYCLE_ROOT,
        now=now,
    )
    state = _load_state(state_path, project.project_id)
    semantic_hash = source_bundle_semantic_hash(source_bundle)
    if state.last_source_semantic_hash != semantic_hash:
        return {
            "status": (
                "manual_refresh_required"
                if args.unattended
                else "artifact_required"
            ),
            "project_id": project.project_id,
        }
    if args.unattended and source_bundle.retrieval_requests:
        return {
            "status": "manual_refresh_required",
            "project_id": project.project_id,
        }
    assert state.last_artifact_hash is not None
    try:
        approved = _read_approved_artifact(context_path, state.last_artifact_hash)
    except ValueError as exc:
        if args.unattended and str(exc) == "approved_artifact_unavailable":
            return {
                "status": "manual_refresh_required",
                "project_id": project.project_id,
            }
        raise
    selected = QwenProjectContextArtifact(
        schema_version=1,
        context=approved.context.model_copy(
            update={"generated_at": source_bundle.collected_at}
        ),
        completed_retrieval_request_ids=(),
    )
    _validate_context(project, source_bundle, selected.context)
    if state.pending is not None:
        pending_envelope = _build_envelope(
            project,
            source_bundle,
            selected.context,
            completed_retrieval_request_ids=(),
            source_cursor=state.pending.source_cursor,
            now=now(),
        )
        if (
            not _pending_identity_matches(state.pending, pending_envelope)
            and not _pending_rebuild_allowed(
                state.pending,
                pending_envelope,
            )
        ):
            raise ValueError("pending_sync_conflict")
    encoded = _canonical_bytes(selected.model_dump(mode="json"))
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("context_file_too_large")
    transaction = _RecoverableAtomicWrite(context_path, encoded)
    lifecycle.commit_stage(
        project.project_id,
        args.run_token,
        expected="pending",
        target="artifact",
        apply=transaction.apply,
        rollback=transaction.rollback,
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {
        "status": "artifact_reused",
        "project_id": project.project_id,
        "output_bytes": len(encoded),
    }


def _restore_approved_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    manifest_path, sources_path, context_path, state_path = (
        _validated_sync_paths(args)
    )
    manifest = DwsManifest.load(manifest_path)
    project = _selected_project(manifest, args.project)
    sources_payload = _read_restore_json_object(
        sources_path,
        "sources_file",
        max_bytes=MAX_PRIVATE_INPUT_BYTES,
    )
    try:
        source_bundle = DwsSourceBundle.model_validate(sources_payload)
    except (TypeError, ValueError):
        raise ValueError("sources_file_invalid") from None
    _validate_bundle(project, source_bundle)
    context_payload = _read_restore_json_object(
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
    _validate_restore_context(project, source_bundle, artifact.context)
    state_payload = _read_restore_json_object(
        state_path,
        "state_file",
        max_bytes=MAX_STATE_BYTES,
    )
    try:
        state = SyncCliState.model_validate(state_payload)
    except (TypeError, ValueError):
        raise ValueError("state_file_invalid") from None
    if state.project_id != project.project_id:
        raise ValueError("state_project_mismatch")
    if (
        state.pending is not None
        or source_bundle.retrieval_requests
        or state.last_source_semantic_hash is None
        or state.last_artifact_hash is None
        or state.last_source_semantic_hash
        != source_bundle_semantic_hash(source_bundle)
    ):
        raise ValueError("approved_restore_denied")
    encoded = _canonical_bytes(artifact.model_dump(mode="json"))
    if len(encoded) > MAX_PRIVATE_INPUT_BYTES:
        raise ValueError("context_file_too_large")
    if not hmac.compare_digest(
        hashlib.sha256(encoded).hexdigest(),
        state.last_artifact_hash,
    ):
        raise ValueError("approved_restore_denied")
    lifecycle.assert_stage(
        project.project_id,
        args.run_token,
        expected="pending",
        root=LIFECYCLE_ROOT,
        now=now,
    )
    try:
        _read_approved_artifact(context_path, state.last_artifact_hash)
    except ValueError as exc:
        if str(exc) != "approved_artifact_unavailable":
            raise
    else:
        lifecycle.apply_stage(
            project.project_id,
            args.run_token,
            expected="pending",
            apply=lambda: None,
            rollback=lambda: None,
            root=LIFECYCLE_ROOT,
            now=now,
        )
        return {"status": "approved_restored", "project_id": project.project_id}
    transaction = _RecoverableAtomicWrite(
        approved_artifact_path(context_path),
        encoded,
    )
    lifecycle.apply_stage(
        project.project_id,
        args.run_token,
        expected="pending",
        apply=transaction.apply,
        rollback=transaction.rollback,
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {"status": "approved_restored", "project_id": project.project_id}


def _read_push_inputs(
    args: argparse.Namespace,
) -> tuple[DwsProjectManifest, DwsSourceBundle, QwenProjectContextArtifact, Path]:
    manifest_path, sources_path, context_path, state_path = (
        _validated_sync_paths(args)
    )
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


_RECOVERY_DATABASE_SIDECARS = ("-wal", "-shm", "-journal")


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _recovery_database_path(raw_path: str, private_paths: tuple[Path, ...]) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError("database_file_not_absolute")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError("database_file_parent_invalid")
    try:
        resolved = path.resolve(strict=True)
        if path.drive.upper() != "E:" or resolved.drive.upper() != "E:":
            raise ValueError("database_file_not_regular_file")
        for current in (*reversed(path.parents), path):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 1024
            ):
                raise ValueError("database_file_not_regular_file")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("database_file_not_regular_file")
        database_paths = (
            resolved,
            *(
                Path(str(resolved) + suffix)
                for suffix in _RECOVERY_DATABASE_SIDECARS
            ),
        )
        for database_candidate in database_paths:
            for private_path in private_paths:
                if _normalized_path(database_candidate) == _normalized_path(
                    private_path
                ) or (
                    database_candidate.exists()
                    and private_path.exists()
                    and database_candidate.samefile(private_path)
                ):
                    raise ValueError("database_file_not_regular_file")
    except ValueError:
        raise
    except OSError:
        raise ValueError("database_file_not_regular_file") from None
    return resolved


def _database_file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _open_recovery_database_handle(path: Path) -> int:
    if os.name != "nt":
        _pending_recovery_denied()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle in {None, ctypes.c_void_p(-1).value}:
            _pending_recovery_denied()
        return int(handle)
    except ValueError:
        raise
    except Exception:
        _pending_recovery_denied()


def _close_recovery_database_handle(handle: int) -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        if not close_handle(ctypes.c_void_p(handle)):
            _pending_recovery_denied()
    except ValueError:
        raise
    except Exception:
        _pending_recovery_denied()


def _read_recovery_database_header(handle: int) -> bytes:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_pointer = kernel32.SetFilePointerEx
        set_pointer.argtypes = (
            ctypes.c_void_p,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.c_uint32,
        )
        set_pointer.restype = ctypes.c_int
        position = ctypes.c_longlong()
        if not set_pointer(
            ctypes.c_void_p(handle),
            ctypes.c_longlong(0),
            ctypes.byref(position),
            0,
        ):
            _pending_recovery_denied()
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        )
        read_file.restype = ctypes.c_int
        buffer = (ctypes.c_ubyte * 20)()
        read_count = ctypes.c_uint32()
        if not read_file(
            ctypes.c_void_p(handle),
            buffer,
            len(buffer),
            ctypes.byref(read_count),
            None,
        ):
            _pending_recovery_denied()
        return bytes(buffer[: read_count.value])
    except ValueError:
        raise
    except Exception:
        _pending_recovery_denied()


@contextmanager
def _recovery_database_guard(path: Path) -> Iterator[int]:
    try:
        handle = _open_recovery_database_handle(path)
    except Exception:
        _pending_recovery_denied()
    try:
        yield handle
    finally:
        _close_recovery_database_handle(handle)


def _recovery_database_snapshot(
    path: Path,
    handle: int,
) -> tuple[int, int, int, int]:
    try:
        path_stat = path.lstat()
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or getattr(path_stat, "st_file_attributes", 0) & 1024
            or path_stat.st_nlink != 1
        ):
            _pending_recovery_denied()
        for suffix in _RECOVERY_DATABASE_SIDECARS:
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                continue
            _pending_recovery_denied()
        header = _read_recovery_database_header(handle)
        final_path_stat = path.lstat()
        if (
            len(header) != 20
            or header[:16] != b"SQLite format 3\0"
            or header[18] != 1
            or header[19] != 1
            or not os.path.samestat(path_stat, final_path_stat)
            or _database_file_identity(path_stat)
            != _database_file_identity(final_path_stat)
        ):
            _pending_recovery_denied()
    except ValueError:
        raise
    except OSError:
        _pending_recovery_denied()
    return _database_file_identity(path_stat)


def _recovery_sqlite_authorizer(
    action: int,
    first: str | None,
    second: str | None,
    _database: str | None,
    _trigger: str | None,
) -> int:
    if action in {
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        if first == "query_only" and second == "ON":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_TRANSACTION and first in {
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
    }:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _pending_recovery_denied() -> None:
    raise ValueError("pending_recovery_denied")


def _load_recovery_state(path: Path, project_id: str) -> SyncCliState:
    payload = _read_json_object(path, "state_file", max_bytes=MAX_STATE_BYTES)
    try:
        state = SyncCliState.model_validate(payload)
    except (TypeError, ValueError) as exc:
        if "state_pending_cursor_invalid" in str(exc):
            _pending_recovery_denied()
        raise ValueError("state_file_invalid") from None
    if state.project_id != project_id:
        raise ValueError("state_project_mismatch")
    return state


def _recovery_source_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _pending_recovery_denied()
    return parsed


def _prove_pending_recovery(
    database_path: Path,
    database_handle: int,
    project: DwsProjectManifest,
    source_bundle: DwsSourceBundle,
    state: SyncCliState,
    *,
    now: datetime,
    expected_database_snapshot: tuple[int, int, int, int],
) -> QwenProjectContextArtifact:
    pending = state.pending
    if pending is None or state.last_cursor < 1 or state.last_content_hash is None:
        _pending_recovery_denied()
    if (
        pending.sync_id
        != _sync_id(
            project.project_id,
            pending.source_cursor,
            pending.content_hash,
        )
    ):
        _pending_recovery_denied()
    if pending.completion_claims_hash != _completion_claims_hash(()):
        _pending_recovery_denied()
    if source_bundle.retrieval_requests or any(
        record.status != "active" for record in source_bundle.records
    ):
        _pending_recovery_denied()

    if (
        _recovery_database_snapshot(database_path, database_handle)
        != expected_database_snapshot
    ):
        _pending_recovery_denied()
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.set_authorizer(_recovery_sqlite_authorizer)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            active_rows = connection.execute(
                """
                SELECT generation.generation_id, generation.source_cursor,
                       generation.content_hash, generation.context_json
                FROM project_active_generations AS active
                JOIN project_sync_generations AS generation
                  ON generation.project_id = active.project_id
                 AND generation.generation_id = active.generation_id
                WHERE active.project_id = ?
                """,
                (project.project_id,),
            ).fetchall()
            if len(active_rows) != 1:
                _pending_recovery_denied()
            generation_id, active_cursor, active_hash, context_json = active_rows[0]
            if (
                active_cursor != state.last_cursor
                or active_hash != state.last_content_hash
            ):
                _pending_recovery_denied()
            generation_count = connection.execute(
                """
                SELECT COUNT(*) FROM project_sync_generations
                WHERE sync_id = ? OR (project_id = ? AND source_cursor = ?)
                """,
                (pending.sync_id, project.project_id, pending.source_cursor),
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT COUNT(*) FROM project_sync_audits
                WHERE sync_id = ?
                """,
                (pending.sync_id,),
            ).fetchone()
            retrieval_count = connection.execute(
                """
                SELECT COUNT(*) FROM project_retrieval_requests
                WHERE project_id = ? AND status = 'in_progress'
                """,
                (project.project_id,),
            ).fetchone()
            if (
                generation_count != (0,)
                or audit_count != (0,)
                or retrieval_count != (0,)
            ):
                _pending_recovery_denied()
            source_rows = connection.execute(
                """
                SELECT source_type, source_id_hash, status, source_version,
                       source_time, content_hash, permission_hash
                FROM project_source_states
                WHERE project_id = ? AND generation_id = ?
                """,
                (project.project_id, generation_id),
            ).fetchall()
    except ValueError:
        raise
    except (OSError, sqlite3.Error):
        _pending_recovery_denied()
    if (
        _recovery_database_snapshot(database_path, database_handle)
        != expected_database_snapshot
    ):
        _pending_recovery_denied()

    expected_sources = {
        (record.source_type.value, _sha256(record.source_id)): record
        for record in source_bundle.records
    }
    if len(expected_sources) != len(source_bundle.records):
        _pending_recovery_denied()
    actual_sources = {(row[0], row[1]): row for row in source_rows}
    if (
        len(actual_sources) != len(source_rows)
        or actual_sources.keys() != expected_sources.keys()
    ):
        _pending_recovery_denied()
    try:
        for identity, record in expected_sources.items():
            row = actual_sources[identity]
            if (
                row[2] != record.status
                or row[3] != record.source_version
                or _recovery_source_time(row[4]) != record.source_time
                or row[5] != record.content_hash
                or row[6] != _sha256(record.permission_scope)
            ):
                _pending_recovery_denied()
        context_payload = json.loads(
            context_json, parse_constant=_reject_non_finite
        )
        context = ProjectContextPackage.model_validate(context_payload).model_copy(
            update={"generated_at": source_bundle.collected_at}
        )
        _validate_context(project, source_bundle, context)
        envelope = _build_envelope(
            project,
            source_bundle,
            context,
            completed_retrieval_request_ids=(),
            source_cursor=state.last_cursor,
            now=now,
        )
    except ValueError as exc:
        if str(exc) == "pending_recovery_denied":
            raise
        _pending_recovery_denied()
    except (TypeError, json.JSONDecodeError, ValidationError):
        _pending_recovery_denied()
    if not hmac.compare_digest(envelope.content_hash, str(active_hash)):
        _pending_recovery_denied()
    return QwenProjectContextArtifact(
        schema_version=1,
        context=context,
        completed_retrieval_request_ids=(),
    )


def _recover_pending_with_database_guard(
    args: argparse.Namespace,
    database_path: Path,
    database_handle: int,
    manifest_path: Path,
    sources_path: Path,
    context_path: Path,
    state_path: Path,
    applied: list[_RecoverableAtomicWrite],
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
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
    _validate_bundle(project, source_bundle)
    state = _load_recovery_state(state_path, project.project_id)
    database_snapshot = _recovery_database_snapshot(
        database_path, database_handle
    )
    artifact = _prove_pending_recovery(
        database_path,
        database_handle,
        project,
        source_bundle,
        state,
        now=now(),
        expected_database_snapshot=database_snapshot,
    )
    pending = state.pending
    assert pending is not None
    encoded = _canonical_bytes(artifact.model_dump(mode="json"))
    promoted = state.model_copy(
        update={
            "pending": None,
            "last_source_semantic_hash": source_bundle_semantic_hash(
                source_bundle
            ),
            "last_artifact_hash": hashlib.sha256(encoded).hexdigest(),
        }
    )
    if (
        _recovery_database_snapshot(database_path, database_handle)
        != database_snapshot
    ):
        _pending_recovery_denied()
    transactions = (
        _RecoverableAtomicWrite(approved_artifact_path(context_path), encoded),
        _RecoverableAtomicWrite(context_path, encoded),
        _RecoverableAtomicWrite(
            state_path, _canonical_bytes(promoted.model_dump())
        ),
    )
    for transaction in transactions:
        applied.append(transaction)
        transaction.apply()
    return {
        "status": "pending_recovered",
        "project_id": project.project_id,
        "active_cursor": state.last_cursor,
        "abandoned_pending_cursor": pending.source_cursor,
    }


def _recover_pending_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    with lifecycle.manual_guard(
        args.project,
        root=LIFECYCLE_ROOT,
        now=now,
        timeout=SYNC_LOCK_TIMEOUT_SECONDS,
    ):
        manifest_path, sources_path, context_path, state_path = (
            _validated_sync_paths(args)
        )
        database_path = _recovery_database_path(
            args.database_file,
            (
                manifest_path,
                sources_path,
                context_path,
                approved_artifact_path(context_path),
                state_path,
            ),
        )
        applied: list[_RecoverableAtomicWrite] = []
        try:
            with _recovery_database_guard(database_path) as database_handle:
                return _recover_pending_with_database_guard(
                    args,
                    database_path,
                    database_handle,
                    manifest_path,
                    sources_path,
                    context_path,
                    state_path,
                    applied,
                    now=now,
                )
        except BaseException:
            try:
                for transaction in reversed(applied):
                    transaction.rollback()
            except BaseException:
                raise ValueError("private_file_write_failed") from None
            raise


def _pending_discard_denied() -> None:
    raise ValueError("pending_discard_denied")


def _discard_database_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 1024
            or info.st_nlink != 1
        ):
            _pending_discard_denied()
    except ValueError:
        raise
    except OSError:
        _pending_discard_denied()
    return _database_file_identity(info)


def _discard_sqlite_authorizer(
    action: int,
    first: str | None,
    second: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    if action == sqlite3.SQLITE_PRAGMA and first == "data_version" and second is None:
        return sqlite3.SQLITE_OK
    return _recovery_sqlite_authorizer(
        action,
        first,
        second,
        database,
        trigger,
    )


def _discard_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if (
        row is None
        or len(row) != 1
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
    ):
        _pending_discard_denied()
    return row[0]


def _discard_rejected_pending_inner(
    args: argparse.Namespace,
    database_path: Path,
    manifest_path: Path,
    state_path: Path,
) -> dict[str, object]:
    manifest = DwsManifest.load(manifest_path)
    project = _selected_project(manifest, args.project)
    state = _load_recovery_state(state_path, project.project_id)
    pending = state.pending
    if pending is None or state.last_cursor < 1:
        _pending_discard_denied()

    database_identity = _discard_database_identity(database_path)
    state_write: _RecoverableAtomicWrite | None = None
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.set_authorizer(_discard_sqlite_authorizer)
            connection.execute("PRAGMA query_only=ON")
            initial_data_version = _discard_data_version(connection)
            connection.execute("BEGIN")
            active_rows = connection.execute(
                """
                SELECT generation.source_cursor, generation.content_hash
                FROM project_active_generations AS active
                JOIN project_sync_generations AS generation
                  ON generation.project_id = active.project_id
                 AND generation.generation_id = active.generation_id
                WHERE active.project_id = ?
                """,
                (project.project_id,),
            ).fetchall()
            if active_rows != [
                (
                    state.last_cursor,
                    state.last_content_hash,
                )
            ]:
                _pending_discard_denied()
            generation_count = connection.execute(
                """
                SELECT COUNT(*) FROM project_sync_generations
                WHERE sync_id = ? OR (project_id = ? AND source_cursor = ?)
                """,
                (pending.sync_id, project.project_id, pending.source_cursor),
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT COUNT(*) FROM project_sync_audits
                WHERE sync_id = ?
                """,
                (pending.sync_id,),
            ).fetchone()
            if generation_count != (0,) or audit_count != (0,):
                _pending_discard_denied()
            if _discard_database_identity(database_path) != database_identity:
                _pending_discard_denied()
            promoted = state.model_copy(update={"pending": None})
            state_write = _RecoverableAtomicWrite(
                state_path,
                _canonical_bytes(promoted.model_dump()),
            )
            state_write.apply()
            connection.execute("COMMIT")
            if (
                _discard_data_version(connection) != initial_data_version
                or _discard_database_identity(database_path) != database_identity
            ):
                _pending_discard_denied()
    except BaseException as exc:
        if state_write is not None:
            try:
                state_write.rollback()
            except BaseException:
                raise ValueError("private_file_write_failed") from None
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _pending_discard_denied()
    return {
        "status": "pending_discarded",
        "project_id": project.project_id,
        "abandoned_pending_cursor": pending.source_cursor,
    }


def _discard_rejected_pending_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    try:
        if args.confirm != "sync_conflict":
            _pending_discard_denied()
        with lifecycle.manual_guard(
            args.project,
            root=LIFECYCLE_ROOT,
            now=now,
            timeout=SYNC_LOCK_TIMEOUT_SECONDS,
        ):
            manifest_path = _absolute_private_path(args.manifest, "manifest")
            state_path = _absolute_private_path(args.state_file, "state_file")
            _validate_distinct_private_paths((manifest_path, state_path))
            database_path = _recovery_database_path(
                args.database_file,
                (manifest_path, state_path),
            )
            return _discard_rejected_pending_inner(
                args,
                database_path,
                manifest_path,
                state_path,
            )
    except ValueError as exc:
        if str(exc) == "private_file_write_failed":
            raise
        _pending_discard_denied()
    except Exception:
        _pending_discard_denied()


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
        rebuild_pending = False
        if state.pending is not None:
            if not _pending_identity_matches(state.pending, envelope):
                if not _pending_rebuild_allowed(state.pending, envelope):
                    raise ValueError("pending_sync_conflict")
                rebuild_pending = True
            if (
                state.pending.sync_id != envelope.sync_id
                and not rebuild_pending
            ):
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
            context_semantic_hash=_context_semantic_hash(envelope.context),
        )
        if state.pending is None or rebuild_pending:
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
        try:
            response_payload = _gateway_request(
                request,
                opener=opener,
                parse=lambda raw: _validate_response(raw, envelope=envelope),
                monotonic=monotonic,
                sleep=sleep,
            )
        except ValueError as exc:
            if str(exc) == "sync_conflict":
                try:
                    _atomic_write(
                        state_path,
                        _canonical_bytes(
                            state.model_copy(
                                update={
                                    "pending": pending.model_copy(
                                        update={"failure_type": "sync_conflict"}
                                    )
                                }
                            ).model_dump()
                        ),
                    )
                except BaseException:
                    raise ValueError("private_file_write_failed") from None
            raise
        assert isinstance(response_payload, dict)
        duration_ms = max(0, int((monotonic() - started) * 1000))
        promoted = SyncCliState(
            schema_version=1,
            project_id=project.project_id,
            last_cursor=envelope.source_cursor,
            last_content_hash=envelope.content_hash,
            last_sync_id=envelope.sync_id,
            pending=None,
            last_source_semantic_hash=source_bundle_semantic_hash(source_bundle),
            last_artifact_hash=hashlib.sha256(
                _canonical_bytes(artifact.model_dump(mode="json"))
            ).hexdigest(),
        )
        transactions = (
            _RecoverableAtomicWrite(
                approved_artifact_path(
                    _absolute_private_path(args.context_file, "context_file")
                ),
                _canonical_bytes(artifact.model_dump(mode="json")),
            ),
            _RecoverableAtomicWrite(
                state_path,
                _canonical_bytes(promoted.model_dump()),
            ),
        )
        applied: list[_RecoverableAtomicWrite] = []
        try:
            for transaction in transactions:
                applied.append(transaction)
                transaction.apply()
        except BaseException:
            try:
                for transaction in reversed(applied):
                    transaction.rollback()
            except BaseException:
                raise ValueError("private_file_write_failed") from None
            raise
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


def _begin_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    started = lifecycle.begin_run_with_cleanup(
        args.project,
        cleanup=lambda: host_capture.discard_document_info_capture(
            args.project,
            protector=_host_capture_protector(),
        ),
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {
        "status": started.status,
        "project_id": args.project,
        "run_token": started.run_token,
    }


def _end_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    def reject_remaining_capture() -> None:
        try:
            if host_capture.document_info_capture_exists(args.project):
                raise ValueError("host_capture_remaining")
        except ValueError as exc:
            if str(exc) == "host_capture_remaining":
                raise
            raise ValueError("host_capture_remaining") from None
        except Exception:
            raise ValueError("host_capture_remaining") from None

    ended = lifecycle.end_run_with_check(
        args.project,
        args.run_token,
        check=reject_remaining_capture,
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {
        "status": ended.status,
        "project_id": args.project,
        "run_token": ended.run_token,
    }


def _abort_command(
    args: argparse.Namespace,
    *,
    now: Callable[[], datetime],
) -> dict[str, object]:
    lifecycle.abort_run_with_cleanup(
        args.project,
        args.run_token,
        cleanup=lambda: host_capture.discard_document_info_capture(
            args.project,
            protector=_host_capture_protector(),
        ),
        root=LIFECYCLE_ROOT,
        now=now,
    )
    return {"status": "aborted", "project_id": args.project}


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
    direct_collection: bool = False,
) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in actual_argv or "-h" in actual_argv:
        if actual_argv and actual_argv[0] in {
            "abort",
            "artifact",
            "begin",
            "collect",
            "capture-info",
            "host-import",
            "complete-host-import",
            "end",
            "pending",
            "discard-rejected-pending",
            "recover-pending",
            "reuse-artifact",
            "restore-approved",
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
                    "capture-info",
                    "host-import",
                    "complete-host-import",
                    "pending",
                    "discard-rejected-pending",
                    "recover-pending",
                    "reuse-artifact",
                    "restore-approved",
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
        if direct_collection and args.command != "collect":
            raise ValueError("arguments_invalid")
        if args.command == "begin":
            output = _begin_command(args, now=now)
        elif args.command == "collect":
            output = _collect_command(
                args,
                runner=runner,
                now=now,
                direct_collection=direct_collection,
            )
        elif args.command == "host-import":
            output = _host_import_command(
                args,
                input_stream=(
                    sys.stdin.buffer if input_stream is None else input_stream
                ),
                now=now,
            )
        elif args.command == "capture-info":
            output = _capture_info_command(
                args,
                input_stream=(
                    sys.stdin.buffer if input_stream is None else input_stream
                ),
                now=now,
            )
        elif args.command == "complete-host-import":
            output = _complete_host_import_command(
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
        elif args.command == "recover-pending":
            output = _recover_pending_command(args, now=now)
        elif args.command == "discard-rejected-pending":
            output = _discard_rejected_pending_command(args, now=now)
        elif args.command == "reuse-artifact":
            output = _reuse_artifact_command(args, now=now)
        elif args.command == "restore-approved":
            output = _restore_approved_command(args, now=now)
        elif args.command == "end":
            output = _end_command(args, now=now)
        elif args.command == "abort":
            output = _abort_command(args, now=now)
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
