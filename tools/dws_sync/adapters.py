from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.project.sync_models import SourceErrorType, SyncSourceType
from tools.dws_sync.manifest import DwsProjectManifest, DwsSourceSpec
from tools.dws_sync.runner import DwsReadError, normalized_read_error


_PAYLOAD_KEYS = ("body", "data", "result")
_WRAPPER_KEYS = {
    "body",
    "code",
    "data",
    "message",
    "nextToken",
    "requestId",
    "result",
    "retry_after_seconds",
    "retryable",
    "success",
    "traceId",
}
_IDENTITY_ALIASES = ("source_id", "nodeId", "taskUuid", "taskId", "eventId", "id")
_TITLE_ALIASES = ("source_title", "title", "name", "summary", "subject")
_URL_ALIASES = ("source_url", "url", "link", "shareUrl")
_VERSION_ALIASES = (
    "source_version",
    "version",
    "revision",
    "updatedAt",
    "updateTime",
)
_TIME_ALIASES = (
    "source_time",
    "updatedAt",
    "updateTime",
    "startTime",
    "createdAt",
    "createTime",
)
_TRANSCRIPTION_KEYS = ("paragraphs", "items", "records")
_MAX_TRANSCRIPTION_PAGES = 100
_MAX_CALENDAR_PAGES = 100
_MISSING = object()


class Runner(Protocol):
    def run(self, args: tuple[str, ...]) -> dict[str, object]: ...


class DwsSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SyncSourceType
    source_id: str = Field(min_length=1, max_length=256)
    permission_scope: str = Field(min_length=1, max_length=256)
    fetched_at: datetime
    status: Literal["active", "failed", "deleted", "revoked"]
    source_title: str | None = Field(default=None, max_length=512)
    source_url: str | None = Field(default=None, max_length=2048)
    source_version: str | None = Field(default=None, max_length=256)
    source_time: datetime | None = None
    content_text: str | None = None
    attributes_json: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
    error_type: SourceErrorType | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)

    @field_validator("source_id", "permission_scope")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("record field must not be blank")
        return value

    @field_validator("fetched_at", "source_time")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("record time must be timezone-aware")
        return value

    @field_validator("retry_after_seconds")
    @classmethod
    def validate_retry_after(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("retry_after_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> "DwsSourceRecord":
        metadata_fields = (
            self.source_title,
            self.source_url,
            self.source_version,
            self.source_time,
        )
        content_fields = (self.content_text, self.attributes_json, self.content_hash)
        if self.status == "active":
            if any(value is None for value in (*metadata_fields, *content_fields)):
                raise ValueError("active record requires all content fields")
            assert self.content_text is not None
            assert self.attributes_json is not None
            if not self.content_text.strip() or not self.attributes_json.strip():
                raise ValueError("active record content must not be blank")
            if self.error_type is not None or self.retryable is not None:
                raise ValueError("active record forbids error fields")
            if self.retry_after_seconds is not None:
                raise ValueError("active record forbids retry fields")
            if self.content_hash != _sha256(self.content_text):
                raise ValueError("content_hash must match content_text")
        elif self.status == "failed":
            if any(value is not None for value in content_fields):
                raise ValueError("failed record forbids content fields")
            if self.error_type is None or self.retryable is None:
                raise ValueError("failed record requires error fields")
            if self.retry_after_seconds is not None and not self.retryable:
                raise ValueError("retry_after_seconds requires retryable failure")
        else:
            if any(value is not None for value in content_fields):
                raise ValueError("terminal record forbids content fields")
            if (
                self.error_type is not None
                or self.retryable is not None
                or self.retry_after_seconds is not None
            ):
                raise ValueError("terminal record forbids error fields")
        return self


class DwsRetrievalSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SyncSourceType
    source_id: str = Field(min_length=1, max_length=256)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_id must not be blank")
        return value


class DwsRetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: tuple[DwsRetrievalSource, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_sources(self) -> "DwsRetrievalRequest":
        identities = {(item.source_type, item.source_id) for item in self.sources}
        if len(identities) != len(self.sources):
            raise ValueError("retrieval source identities must be unique")
        return self


class DwsSourceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=512)
    permission_scope: str = Field(min_length=1, max_length=256)
    collected_at: datetime
    records: tuple[DwsSourceRecord, ...] = Field(min_length=1, max_length=30)
    retrieval_requests: tuple[DwsRetrievalRequest, ...] = Field(
        default=(),
        max_length=100,
    )
    content_hash: str = Field(pattern=r"[0-9a-f]{64}")

    @field_validator("collected_at")
    @classmethod
    def validate_collected_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")
        return value

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        invalid_character = any(
            character not in "0123456789abcdef" for character in value
        )
        if len(value) != 64 or invalid_character:
            raise ValueError("content_hash must be a lowercase SHA-256 hash")
        return value

    @model_validator(mode="after")
    def validate_bundle(self) -> "DwsSourceBundle":
        identities = {(item.source_type, item.source_id) for item in self.records}
        if len(identities) != len(self.records):
            raise ValueError("record identities must be unique")
        if any(item.permission_scope != self.permission_scope for item in self.records):
            raise ValueError("record permission scope must match bundle")
        request_ids = [item.request_id for item in self.retrieval_requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("retrieval request IDs must be unique")
        requested = {
            (source.source_type, source.source_id)
            for request in self.retrieval_requests
            for source in request.sources
        }
        if not requested.issubset(identities):
            raise ValueError("retrieval sources must belong to bundle")
        return self


def unwrap_dws_payload(response: Mapping[str, object]) -> object:
    if not isinstance(response, Mapping):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if "success" in response and response["success"] is not True:
        raise normalized_read_error(response)

    payload: object = response
    for _depth in range(3):
        if not isinstance(payload, Mapping):
            if isinstance(payload, list):
                return payload
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        if "success" in payload and payload["success"] is not True:
            raise normalized_read_error(payload)
        present = [key for key in _PAYLOAD_KEYS if key in payload]
        if not present:
            return payload
        if len(present) != 1 or not set(payload).issubset(_WRAPPER_KEYS):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        payload = payload[present[0]]
        if not isinstance(payload, (Mapping, list)):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)

    if isinstance(payload, Mapping) and any(key in payload for key in _PAYLOAD_KEYS):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if not isinstance(payload, (Mapping, list)):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return payload


def read_document(
    runner: Runner,
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    clock: Callable[[], datetime],
) -> DwsSourceRecord:
    _require_source_type(spec, SyncSourceType.DOCUMENT)
    info = _require_mapping(
        unwrap_dws_payload(
            runner.run(("doc", "info", "--node", spec.source_id))
        )
    )
    _validate_identity(info, spec.source_id)
    content_type = info.get("contentType")
    extension = info.get("extension", info.get("type"))
    if content_type != "ALIDOC" or extension != "adoc":
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    read = _require_mapping(
        unwrap_dws_payload(
            runner.run(("doc", "read", "--node", spec.source_id))
        )
    )
    markdown = read.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return _active_record(
        spec,
        permission_scope=permission_scope,
        fetched_at=_read_clock(clock),
        metadata=info,
        content_text=markdown,
        attributes={"info": info, "read": read},
    )


def read_meeting_note(
    runner: Runner,
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    clock: Callable[[], datetime],
) -> DwsSourceRecord:
    _require_source_type(spec, SyncSourceType.MEETING_NOTE)
    info = _require_mapping(
        unwrap_dws_payload(
            runner.run(("minutes", "get", "info", "--id", spec.source_id))
        )
    )
    _validate_identity(info, spec.source_id)
    summary = unwrap_dws_payload(
        runner.run(("minutes", "get", "summary", "--id", spec.source_id))
    )

    transcription: list[object] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    for page_index in range(_MAX_TRANSCRIPTION_PAGES):
        args = ("minutes", "get", "transcription", "--id", spec.source_id)
        if next_token is not None:
            args += ("--next-token", next_token)
        page = _require_mapping(unwrap_dws_payload(runner.run(args)))
        present = [key for key in _TRANSCRIPTION_KEYS if key in page]
        if len(present) != 1 or not set(page).issubset(
            {present[0], "nextToken"}
        ):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        items = page[present[0]]
        if not isinstance(items, list):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        token_value = page.get("nextToken")
        if token_value in (None, ""):
            next_token = None
        elif isinstance(token_value, str):
            next_token = token_value
        else:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        if not items and next_token is not None:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        transcription.extend(items)
        if next_token is None:
            break
        if next_token in seen_tokens or page_index == _MAX_TRANSCRIPTION_PAGES - 1:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        seen_tokens.add(next_token)

    todos = unwrap_dws_payload(
        runner.run(("minutes", "get", "todos", "--id", spec.source_id))
    )
    content = {
        "info": info,
        "summary": summary,
        "transcription": transcription,
        "todos": todos,
    }
    return _active_record(
        spec,
        permission_scope=permission_scope,
        fetched_at=_read_clock(clock),
        metadata=info,
        content_text=_canonical_json(content),
        attributes=info,
    )


def read_task(
    runner: Runner,
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    clock: Callable[[], datetime],
) -> DwsSourceRecord:
    _require_source_type(spec, SyncSourceType.TASK)
    detail = _require_mapping(
        unwrap_dws_payload(
            runner.run(("todo", "task", "get", "--task-id", spec.source_id))
        )
    )
    _validate_identity(detail, spec.source_id)
    return _active_record(
        spec,
        permission_scope=permission_scope,
        fetched_at=_read_clock(clock),
        metadata=detail,
        content_text=_canonical_json(detail),
        attributes=detail,
    )


def read_calendar_event(
    runner: Runner,
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    clock: Callable[[], datetime],
) -> DwsSourceRecord:
    _require_source_type(spec, SyncSourceType.CALENDAR)
    assert spec.window_start is not None
    assert spec.window_end is not None
    fetched_at = _read_clock(clock)
    base_args = (
        "calendar",
        "event",
        "list",
        "--start",
        spec.window_start.isoformat(),
        "--end",
        spec.window_end.isoformat(),
    )
    matches: list[Mapping[str, object]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    for page_index in range(_MAX_CALENDAR_PAGES):
        args = base_args
        if next_token is not None:
            args += ("--next-token", next_token)
        page_events, next_token = _calendar_page(
            unwrap_dws_payload(runner.run(args))
        )
        if not page_events and next_token is not None:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        matches.extend(
            event
            for event in page_events
            if _metadata_value(event, _IDENTITY_ALIASES) == spec.source_id
        )
        if next_token is None:
            break
        if (
            next_token in seen_tokens
            or page_index == _MAX_CALENDAR_PAGES - 1
        ):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        seen_tokens.add(next_token)
    if not matches:
        return _terminal_record(
            spec,
            permission_scope=permission_scope,
            fetched_at=fetched_at,
            status="deleted",
        )
    if len(matches) != 1:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    detail = _require_mapping(
        unwrap_dws_payload(
            runner.run(("calendar", "event", "get", "--id", spec.source_id))
        )
    )
    _validate_identity(detail, spec.source_id)
    return _active_record(
        spec,
        permission_scope=permission_scope,
        fetched_at=fetched_at,
        metadata=detail,
        content_text=_canonical_json(detail),
        attributes=detail,
    )


def collect_sources(
    project: DwsProjectManifest,
    runner: Runner,
    clock: Callable[[], datetime],
) -> DwsSourceBundle:
    collected_at = _read_clock(clock)
    adapters = {
        SyncSourceType.DOCUMENT: read_document,
        SyncSourceType.MEETING_NOTE: read_meeting_note,
        SyncSourceType.TASK: read_task,
        SyncSourceType.CALENDAR: read_calendar_event,
    }
    records: list[DwsSourceRecord] = []
    for spec in project.sources:
        try:
            record = adapters[spec.source_type](
                runner,
                spec,
                permission_scope=project.permission_scope,
                clock=lambda: collected_at,
            )
        except DwsReadError as error:
            if error.error_type is SourceErrorType.NODE_NOT_FOUND:
                record = _terminal_record(
                    spec,
                    permission_scope=project.permission_scope,
                    fetched_at=collected_at,
                    status="deleted",
                )
            elif error.error_type is SourceErrorType.PERMISSION_DENIED:
                record = _terminal_record(
                    spec,
                    permission_scope=project.permission_scope,
                    fetched_at=collected_at,
                    status="revoked",
                )
            else:
                record = _build_record(
                    source_type=spec.source_type,
                    source_id=spec.source_id,
                    permission_scope=project.permission_scope,
                    fetched_at=collected_at,
                    status="failed",
                    error_type=error.error_type,
                    retryable=error.retryable,
                    retry_after_seconds=error.retry_after_seconds,
                )
        records.append(record)

    hash_payload = {
        "schema_version": 1,
        "project_id": project.project_id,
        "project_name": project.project_name,
        "permission_scope": project.permission_scope,
        "collected_at": collected_at.isoformat(),
        "records": [record.model_dump(mode="json") for record in records],
    }
    return DwsSourceBundle(
        **hash_payload,
        content_hash=_sha256(_canonical_json(hash_payload)),
    )


def _calendar_page(
    payload: object,
) -> tuple[list[Mapping[str, object]], str | None]:
    if isinstance(payload, Mapping):
        if (
            "events" not in payload
            or not isinstance(payload["events"], list)
            or not set(payload).issubset({"events", "nextToken"})
        ):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        raw_events = payload["events"]
        token_value = payload.get("nextToken")
        if token_value in (None, ""):
            next_token = None
        elif isinstance(token_value, str):
            next_token = token_value
        else:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    elif isinstance(payload, list):
        raw_events = payload
        next_token = None
    else:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if any(not isinstance(event, Mapping) for event in raw_events):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return list(raw_events), next_token  # type: ignore[arg-type]


def _active_record(
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    fetched_at: datetime,
    metadata: Mapping[str, object],
    content_text: str,
    attributes: object,
) -> DwsSourceRecord:
    content_hash = _sha256(content_text)
    title_value = _metadata_value(metadata, _TITLE_ALIASES)
    url_value = _metadata_value(metadata, _URL_ALIASES)
    version_value = _metadata_value(metadata, _VERSION_ALIASES)
    time_value = _metadata_value(metadata, _TIME_ALIASES)
    source_title = _optional_text(title_value)
    source_url = _optional_text(url_value)
    source_version = _optional_text(version_value)
    source_time = _optional_datetime(time_value)
    return _build_record(
        source_type=spec.source_type,
        source_id=spec.source_id,
        permission_scope=permission_scope,
        fetched_at=fetched_at,
        status="active",
        source_title=source_title or f"{spec.source_type.value}:{spec.source_id}",
        source_url=source_url
        or f"dingtalk://{spec.source_type.value}/{_sha256(spec.source_id)}",
        source_version=source_version or content_hash,
        source_time=source_time or fetched_at,
        content_text=content_text,
        attributes_json=_canonical_json(attributes),
        content_hash=content_hash,
    )


def _terminal_record(
    spec: DwsSourceSpec,
    *,
    permission_scope: str,
    fetched_at: datetime,
    status: Literal["deleted", "revoked"],
) -> DwsSourceRecord:
    return _build_record(
        source_type=spec.source_type,
        source_id=spec.source_id,
        permission_scope=permission_scope,
        fetched_at=fetched_at,
        status=status,
    )


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return value


def _require_source_type(spec: DwsSourceSpec, expected: SyncSourceType) -> None:
    if spec.source_type is not expected:
        raise ValueError("dws_source_type_mismatch")


def _validate_identity(payload: Mapping[str, object], expected: str) -> None:
    identity = _metadata_value(payload, _IDENTITY_ALIASES)
    if identity is not _MISSING and identity != expected:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)


def _metadata_value(
    payload: Mapping[str, object],
    aliases: tuple[str, ...],
) -> object:
    for alias in aliases:
        if alias in payload:
            return payload[alias]
    return _MISSING


def _optional_text(value: object) -> str | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    text = str(value)
    if not text.strip():
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return text


def _optional_datetime(value: object) -> datetime | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) >= 10_000_000_000:
            timestamp /= 1000
        conversion_failed = False
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            conversion_failed = True
        if conversion_failed:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if not isinstance(value, str):
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    parse_failed = False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parse_failed = True
    if parse_failed:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return parsed


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dws_clock_must_be_timezone_aware")
    return value


def _canonical_json(value: object) -> str:
    serialization_failed = False
    try:
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        serialization_failed = True
    if serialization_failed:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return serialized


def _sha256(value: str) -> str:
    encoding_failed = False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return hashlib.sha256(encoded).hexdigest()


def _build_record(**values: object) -> DwsSourceRecord:
    validation_failed = False
    try:
        record = DwsSourceRecord(**values)
    except (TypeError, ValueError):
        validation_failed = True
    if validation_failed:
        raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
    return record
