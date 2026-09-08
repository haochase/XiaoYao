from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from companion_gateway.project.sync_models import SourceErrorType, SyncSourceType
from tools.dws_sync.adapters import (
    DwsSourceBundle,
    build_source_bundle,
    document_metadata_contract,
    read_document,
    unwrap_dws_payload,
)
from tools.dws_sync.manifest import DwsProjectManifest
from tools.dws_sync.runner import DwsReadError


MAX_HOST_IMPORT_BYTES = 2_800_000
MAX_RESULT_BYTES = 2_097_152
MAX_TOTAL_RESULT_BYTES = 2_097_152
_OPERATIONS = ("doc_info", "doc_read")
_PLACEHOLDER_PREFIX = b"[dws-bash:pending-post-tool-use]:"


def _reject_constant(_value: str) -> None:
    raise ValueError("host_import_invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("host_import_invalid")
        result[key] = value
    return result


def _decode_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError("host_import_invalid") from None


def _decode_result(
    item: object,
    expected_operation: Literal["doc_info", "doc_read"],
) -> tuple[dict[str, object], int]:
    if not isinstance(item, dict):
        raise ValueError("host_import_invalid")
    keys = set(item)
    string_payload = keys == {
        "operation",
        "encoding",
        "byte_count",
        "payload",
    }
    chunked_payload = keys == {
        "operation",
        "encoding",
        "byte_count",
        "payload_chunks",
    }
    if (
        not (string_payload or chunked_payload)
        or item["operation"] != expected_operation
        or item["encoding"] != "base64-json"
        or type(item["byte_count"]) is not int
        or not 1 <= item["byte_count"] <= MAX_RESULT_BYTES
    ):
        raise ValueError("host_import_invalid")
    if string_payload:
        payload = item["payload"]
        if not isinstance(payload, str):
            raise ValueError("host_import_invalid")
    else:
        chunks = item["payload_chunks"]
        if (
            not isinstance(chunks, list)
            or not chunks
            or any(
                not isinstance(chunk, str) or not 1 <= len(chunk) <= 64
                for chunk in chunks
            )
        ):
            raise ValueError("host_import_invalid")
        payload = "".join(chunks)
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("host_import_invalid") from None
    if len(decoded) != item["byte_count"]:
        raise ValueError("host_import_invalid")
    if decoded.lstrip().startswith(_PLACEHOLDER_PREFIX):
        raise ValueError("host_import_invalid")
    response = _decode_json(decoded)
    if not isinstance(response, dict):
        raise ValueError("host_import_invalid")
    return response, len(decoded)


def decode_host_result(
    raw: bytes,
    expected_operation: Literal["doc_info", "doc_read"],
) -> dict[str, object]:
    if (
        expected_operation not in _OPERATIONS
        or not isinstance(raw, bytes)
        or len(raw) > MAX_HOST_IMPORT_BYTES
    ):
        raise ValueError("host_import_invalid")
    return _decode_result(_decode_json(raw), expected_operation)[0]


def _decode_results(raw: bytes, project_id: str) -> tuple[dict[str, object], ...]:
    if not isinstance(raw, bytes) or len(raw) > MAX_HOST_IMPORT_BYTES:
        raise ValueError("host_import_invalid")
    envelope = _decode_json(raw)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"schema_version", "project_id", "results"}
        or envelope["schema_version"] != 1
        or type(envelope["schema_version"]) is not int
        or envelope["project_id"] != project_id
    ):
        raise ValueError("host_import_invalid")
    results = envelope["results"]
    if not isinstance(results, list) or len(results) != len(_OPERATIONS):
        raise ValueError("host_import_invalid")

    decoded_results: list[dict[str, object]] = []
    total_bytes = 0
    for expected_operation, item in zip(_OPERATIONS, results, strict=True):
        response, result_bytes = _decode_result(item, expected_operation)
        total_bytes += result_bytes
        if total_bytes > MAX_TOTAL_RESULT_BYTES:
            raise ValueError("host_import_invalid")
        decoded_results.append(response)
    return tuple(decoded_results)


class _CachedDocumentRunner:
    def __init__(
        self,
        source_id: str,
        responses: tuple[Mapping[str, object], ...],
    ):
        self._calls = (
            ("doc", "info", "--node", source_id),
            ("doc", "read", "--node", source_id),
        )
        self._responses = responses
        self._index = 0

    def run(self, args: tuple[str, ...]) -> Mapping[str, object]:
        if self._index >= len(self._calls) or args != self._calls[self._index]:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
        response = self._responses[self._index]
        self._index += 1
        return response

    def assert_complete(self) -> None:
        if self._index != len(self._calls):
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)


def import_single_document_bundle(
    raw: bytes,
    project: DwsProjectManifest,
    *,
    collected_at: datetime,
) -> DwsSourceBundle:
    if (
        len(project.sources) != 1
        or project.sources[0].source_type is not SyncSourceType.DOCUMENT
        or collected_at.tzinfo is None
        or collected_at.utcoffset() is None
    ):
        raise ValueError("host_import_invalid")
    responses = _decode_results(raw, project.project_id)
    return build_single_document_bundle(
        responses[0],
        responses[1],
        project,
        collected_at=collected_at,
    )


def decode_host_results(
    raw: bytes,
    project_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    results = _decode_results(raw, project_id)
    return results[0], results[1]


def build_single_document_bundle(
    document_info: Mapping[str, object],
    document_read: Mapping[str, object],
    project: DwsProjectManifest,
    *,
    collected_at: datetime,
) -> DwsSourceBundle:
    if (
        len(project.sources) != 1
        or project.sources[0].source_type is not SyncSourceType.DOCUMENT
        or collected_at.tzinfo is None
        or collected_at.utcoffset() is None
    ):
        raise ValueError("host_import_invalid")
    spec = project.sources[0]
    plain_info = _plain_json_object(document_info)
    plain_read = _plain_json_object(document_read)
    read_payload = unwrap_dws_payload(plain_read)
    if isinstance(read_payload, Mapping):
        identity_present, identity_matches, _metadata_matches = (
            document_metadata_contract(read_payload, spec.source_id)
        )
        if identity_present and not identity_matches:
            raise ValueError("host_import_invalid")
    runner = _CachedDocumentRunner(
        spec.source_id,
        (plain_info, plain_read),
    )
    record = read_document(
        runner,
        spec,
        permission_scope=project.permission_scope,
        clock=lambda: collected_at,
        sleep=lambda _seconds: None,
    )
    runner.assert_complete()
    return build_source_bundle(project, (record,), collected_at=collected_at)


def _plain_json_object(value: Mapping[str, object]) -> dict[str, object]:
    def copy(item: object) -> object:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("host_import_invalid")
            return {key: copy(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [copy(child) for child in item]
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        raise ValueError("host_import_invalid")

    copied = copy(value)
    if not isinstance(copied, dict):
        raise ValueError("host_import_invalid")
    try:
        json.dumps(copied, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("host_import_invalid") from None
    return copied
