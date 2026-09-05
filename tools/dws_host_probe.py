from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "gateway" / "src"))
    sys.path.insert(0, str(ROOT))

from tools.dws_sync.adapters import document_metadata_contract, unwrap_dws_payload
from tools.dws_sync.manifest import DwsManifest
from tools.dws_sync.runtime import CONFIG_NAME, TaskConfig, read_object


MAX_RESPONSE_BYTES = 2_097_152
MAX_TRANSPORT_BYTES = 2_800_000


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid_constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _decode(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def _kind(value: object) -> str:
    if value is None:
        return "missing_or_null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "scalar"


def probe(raw: bytes, operation: str, expected_source_id: str) -> dict[str, object]:
    if operation not in {"doc_info", "doc_read"}:
        raise ValueError("operation_invalid")
    if len(raw) > MAX_TRANSPORT_BYTES:
        raise ValueError("transport_too_large")
    envelope = _decode(raw)
    if not isinstance(envelope, dict) or set(envelope) != {"encoding", "byte_count", "payload"}:
        raise ValueError("transport_invalid")
    count = envelope["byte_count"]
    if (envelope["encoding"] != "base64-json" or type(count) is not int
            or not 1 <= count <= MAX_RESPONSE_BYTES or not isinstance(envelope["payload"], str)):
        raise ValueError("transport_invalid")
    try:
        decoded = base64.b64decode(envelope["payload"], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("transport_invalid") from None
    if len(decoded) != count:
        raise ValueError("transport_length_mismatch")
    response = _decode(decoded)
    if not isinstance(response, dict):
        raise ValueError("response_not_object")
    unwrapped = unwrap_dws_payload(response)
    result: dict[str, object] = {
        "status": "probed", "operation": operation, "decoded_bytes": count,
        "byte_count_matches": True, "response_is_object": True,
        "unwrapped_kind": _kind(unwrapped), "document_contract_matches": False,
    }
    if not isinstance(unwrapped, dict):
        return result
    if operation == "doc_info":
        identity_present, identity_matches, metadata_matches = (
            document_metadata_contract(unwrapped, expected_source_id)
        )
        result["identity_present"] = identity_present
        result["identity_matches"] = identity_matches
        result["document_contract_matches"] = metadata_matches
    else:
        markdown = unwrapped.get("markdown")
        result["markdown_kind"] = _kind(markdown)
        result["content_kind"] = _kind(unwrapped.get("content"))
        result["text_kind"] = _kind(unwrapped.get("text"))
        result["markdown_chars"] = len(markdown) if isinstance(markdown, str) else 0
        result["document_contract_matches"] = (
            isinstance(markdown, str) and bool(markdown.strip())
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only host-result stdin probe")
    parser.add_argument("operation", choices=("doc_info", "doc_read"))
    args = parser.parse_args()
    try:
        config = TaskConfig.model_validate(read_object(ROOT / CONFIG_NAME))
        project = next(item for item in DwsManifest.load(config.manifest).projects
                       if item.project_id == config.project)
        if len(project.sources) != 1 or project.sources[0].source_type.value != "document":
            raise ValueError("single_document_required")
        result = probe(sys.stdin.buffer.read(MAX_TRANSPORT_BYTES + 1), args.operation,
                       project.sources[0].source_id)
    except Exception:
        print(json.dumps({"status": "blocked", "error_type": "host_probe_invalid"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
