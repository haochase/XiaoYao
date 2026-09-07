from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from tools.dws_sync import host_bridge
from tools.dws_sync.manifest import DwsProjectManifest, DwsSourceSpec


class Protector:
    def __init__(self) -> None:
        self.plaintexts: list[bytes] = []

    def protect(self, project_id: str, plaintext: bytes) -> bytes:
        self.plaintexts.append(plaintext)
        key = hashlib.sha256(project_id.encode("utf-8")).digest()
        return b"capture-v1\0" + bytes(
            value ^ key[index % len(key)]
            for index, value in enumerate(plaintext)
        )

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        prefix = b"capture-v1\0"
        if not protected.startswith(prefix):
            raise RuntimeError("protected_capture_invalid")
        key = hashlib.sha256(project_id.encode("utf-8")).digest()
        return bytes(
            value ^ key[index % len(key)]
            for index, value in enumerate(protected[len(prefix) :])
        )


def manifest(
    *,
    project_id: str = "project-1",
    source_id: str = "doc-1",
) -> DwsProjectManifest:
    return DwsProjectManifest(
        project_id=project_id,
        project_name="Test project",
        profile="private-profile",
        permission_scope="project:project-1",
        sources=(
            DwsSourceSpec(source_type="document", source_id=source_id),
        ),
    )


def document_info(
    *,
    source_id: str = "doc-1",
    title: str = "Title",
) -> dict[str, object]:
    return {
        "result": {
            "nodeId": source_id,
            "contentType": "ALIDOC",
            "extension": "adoc",
            "title": title,
        }
    }


def encoded_result(operation: str, payload: object) -> bytes:
    decoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return json.dumps(
        {
            "operation": operation,
            "encoding": "base64-json",
            "byte_count": len(decoded),
            "payload": base64.b64encode(decoded).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def host_capture_module():
    return importlib.import_module("tools.dws_sync.host_capture")


@pytest.fixture
def capture(tmp_path: Path, monkeypatch):
    module = host_capture_module()
    root = tmp_path / ".private" / "dws-host-captures"
    monkeypatch.setattr(module, "PRIVATE_CAPTURE_ROOT", root)
    monkeypatch.setattr(module, "_TEST_CAPTURE_ROOT", root, raising=False)
    return module


def encoded_bytes(operation: str, decoded: bytes) -> bytes:
    return json.dumps(
        {
            "operation": operation,
            "encoding": "base64-json",
            "byte_count": len(decoded),
            "payload": base64.b64encode(decoded).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_decode_host_result_accepts_one_exact_document_info_result() -> None:
    payload = document_info()

    assert host_bridge.decode_host_result(
        encoded_result("doc_info", payload), "doc_info"
    ) == payload


@pytest.mark.parametrize(
    "raw",
    [
        b'\xef\xbb\xbf{"operation":"doc_info"}',
        b'{"operation":"doc_info","operation":"doc_info"}',
        b'{"operation":"doc_info","encoding":"base64-json",'
        b'"byte_count":1,"payload":"e30="} trailing',
        b'{"operation":"doc_info","encoding":"base64-json",'
        b'"byte_count":true,"payload":"e30="}',
        b'{"operation":"doc_info","encoding":"base64-json",'
        b'"byte_count":2,"payload":"not-base64!"}',
    ],
)
def test_decode_host_result_rejects_strict_contract_violations(raw: bytes) -> None:
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        host_bridge.decode_host_result(raw, "doc_info")


def test_decode_host_result_rejects_nonfinite_decoded_json_after_length_check(
    monkeypatch,
) -> None:
    decoded = b'{"value":NaN}'
    raw = encoded_bytes("doc_info", decoded)
    calls: list[bytes] = []
    original = host_bridge._decode_json

    def record(value: bytes) -> object:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(host_bridge, "_decode_json", record)
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        host_bridge.decode_host_result(raw, "doc_info")

    assert calls == [raw, decoded]


def test_decode_host_result_rejects_pending_placeholder_before_json_decode(
    monkeypatch,
) -> None:
    decoded = b"[dws-bash:pending-post-tool-use]:placeholder"
    raw = encoded_bytes("doc_info", decoded)
    calls: list[bytes] = []
    original = host_bridge._decode_json

    def record(value: bytes) -> object:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(host_bridge, "_decode_json", record)
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        host_bridge.decode_host_result(raw, "doc_info")

    assert calls == [raw]


@pytest.mark.parametrize(
    "root",
    (
        Path(r"E:\haochase\xiaoqian\..\outside"),
        Path(r"E:\haochase\xiaoqian."),
        Path(r"E:\haochase\xiaoqian "),
        Path(r"E:\untrusted-capture-root"),
    ),
)
def test_capture_rejects_root_aliases_and_untrusted_roots(root: Path) -> None:
    module = host_capture_module()

    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        module.document_info_capture_path("project-1", root=root)


def test_capture_uses_private_test_root(capture) -> None:
    path = capture.document_info_capture_path("project-1")

    assert path.parent == capture._capture_root()


def test_capture_roundtrip_keeps_only_protected_document_info(
    capture,
) -> None:
    protector = Protector()
    raw = encoded_result("doc_info", document_info())

    stored = capture.capture_document_info(
        raw,
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    loaded = capture.load_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    path = capture.document_info_capture_path("project-1")

    assert stored == loaded
    assert stored.document_info == document_info()
    with pytest.raises(TypeError):
        stored.document_info["unexpected"] = "value"  # type: ignore[index]
    capture_bytes = path.read_bytes()
    input_payload = json.loads(raw)["payload"]
    assert isinstance(input_payload, str)
    for value in (raw, input_payload.encode("ascii")):
        assert value not in capture_bytes
    assert len(protector.plaintexts) == 1
    assert b"doc_read" not in protector.plaintexts[0]
    assert raw not in protector.plaintexts[0]
    assert input_payload.encode("ascii") not in protector.plaintexts[0]


def test_capture_projects_metadata_before_protection_and_on_load(capture) -> None:
    protector = Protector()
    payload = document_info()
    metadata = payload["result"]
    assert isinstance(metadata, dict)
    metadata.update(
        {
            "markdown": "body-marker",
            "content": "content-marker",
            "text": "text-marker",
            "payload": "payload-marker",
            "contentBase64": "encoded-marker",
            "base64": "base64-marker",
            "logId": "log-marker",
        }
    )
    expected = document_info()

    captured = capture.capture_document_info(
        encoded_result("doc_info", payload),
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    loaded = capture.load_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    protected_payload = json.loads(protector.plaintexts[0])

    assert captured.document_info == expected
    assert loaded.document_info == expected
    assert protected_payload["document_info"] == expected
    assert set(protected_payload["document_info"]["result"]) == {
        "nodeId",
        "contentType",
        "extension",
        "title",
    }


@pytest.mark.parametrize(
    "time_field",
    (
        "source_time",
        "updatedAt",
        "updateTime",
        "startTime",
        "createdAt",
        "createTime",
    ),
)
def test_capture_time_aliases_are_idempotent_and_clearable(
    capture,
    time_field: str,
) -> None:
    protector = Protector()
    payload = document_info()
    metadata = payload["result"]
    assert isinstance(metadata, dict)
    metadata[time_field] = "2026-09-06T16:30:00+08:00"
    raw = encoded_result("doc_info", payload)

    captured = capture.capture_document_info(
        raw,
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    retried = capture.capture_document_info(
        raw,
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    loaded = capture.load_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )

    for value in (captured, retried, loaded):
        result = value.document_info["result"]
        assert isinstance(result, dict | type(value.document_info))
        assert result["source_time"] == "2026-09-06T08:30:00+00:00"
        assert "updatedAt" not in result
    assert capture.clear_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    ) is True


def test_capture_rejects_invalid_document_identity_and_contract(
    capture,
) -> None:
    protector = Protector()
    invalid_payloads = (
        document_info(source_id="other-document"),
        {
            "result": {
                "nodeId": "doc-1",
                "contentType": "DOCX",
                "extension": "adoc",
            }
        },
        {"result": {"contentType": "ALIDOC", "extension": "adoc"}},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValueError, match="^host_capture_invalid$"):
            capture.capture_document_info(
                encoded_result("doc_info", payload),
                manifest(),
                run_token="run-token-1",
                protector=protector,
            )


def test_capture_is_idempotent_and_fails_closed_on_binding_mismatch(
    capture,
) -> None:
    protector = Protector()
    first = encoded_result("doc_info", document_info())

    capture.capture_document_info(
        first,
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    second = capture.capture_document_info(
        first,
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )

    assert second.document_info == document_info()
    with pytest.raises(ValueError, match="^host_capture_conflict$"):
        capture.capture_document_info(
            encoded_result("doc_info", document_info(title="Changed")),
            manifest(),
            run_token="run-token-1",
            protector=protector,
        )
    for selected_manifest, token in (
        (manifest(), "other-run-token"),
        (manifest(source_id="other-document"), "run-token-1"),
        (manifest(project_id="other-project"), "run-token-1"),
    ):
        with pytest.raises(ValueError):
            capture.load_document_info_capture(
                selected_manifest,
                run_token=token,
                protector=protector,
            )


def test_capture_sanitizes_protector_failure_without_writing_capture(
    capture,
) -> None:
    class FailingProtector(Protector):
        def protect(self, project_id: str, plaintext: bytes) -> bytes:
            raise RuntimeError("protector_failed")

    path = capture.document_info_capture_path("project-1")
    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        capture.capture_document_info(
            encoded_result("doc_info", document_info()),
            manifest(),
            run_token="run-token-1",
            protector=FailingProtector(),
        )
    assert not path.exists()


def test_clear_capture_requires_matching_binding_and_is_idempotent(
    capture,
) -> None:
    protector = Protector()
    capture.capture_document_info(
        encoded_result("doc_info", document_info()),
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )

    with pytest.raises(ValueError):
        capture.clear_document_info_capture(
            manifest(),
            run_token="other-run-token",
            protector=protector,
        )
    assert capture.clear_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    ) is True
    assert capture.clear_document_info_capture(
        manifest(),
        run_token="run-token-1",
        protector=protector,
    ) is False


def test_capture_rejects_hardlinked_capture_file(capture) -> None:
    protector = Protector()
    capture.capture_document_info(
        encoded_result("doc_info", document_info()),
        manifest(),
        run_token="run-token-1",
        protector=protector,
    )
    path = capture.document_info_capture_path("project-1")
    alias = path.parent / "capture-alias"
    os.link(path, alias)

    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        capture.load_document_info_capture(
            manifest(),
            run_token="run-token-1",
            protector=protector,
        )


def test_prepared_delete_rejects_same_bytes_replacement_without_deleting_it(
    capture,
) -> None:
    protector = Protector()
    selected = manifest()
    capture.capture_document_info(
        encoded_result("doc_info", document_info()),
        selected,
        run_token="run-token-1",
        protector=protector,
    )
    path = capture.document_info_capture_path("project-1")
    original = path.read_bytes()
    transaction = capture.prepare_document_info_capture_delete(
        selected,
        run_token="run-token-1",
        protector=protector,
    )
    transaction.load()
    path.unlink()
    path.write_bytes(original)

    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        transaction.apply()

    assert path.read_bytes() == original


def test_discard_allows_unknown_old_token_but_rejects_invalid_payload(
    capture,
) -> None:
    protector = Protector()
    selected = manifest()
    capture.capture_document_info(
        encoded_result("doc_info", document_info()),
        selected,
        run_token="old-run-token",
        protector=protector,
    )

    assert capture.discard_document_info_capture(
        "project-1",
        protector=protector,
    ) is True

    path = capture.document_info_capture_path("project-1")
    original = b"not-a-protected-host-capture"
    path.write_bytes(original)
    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        capture.discard_document_info_capture(
            "project-1",
            protector=protector,
        )
    assert path.read_bytes() == original


def test_capture_write_rollback_preserves_same_bytes_replacement_after_publish(
    capture,
    monkeypatch,
) -> None:
    protector = Protector()
    selected = manifest()
    transaction = capture.prepare_document_info_capture(
        document_info(),
        selected,
        run_token="run-token-1",
        protector=protector,
    )
    path = capture.document_info_capture_path("project-1")
    real_replace = capture.os.replace
    staged_info = None
    replacement_bytes = b""

    def replace_then_substitute(source, destination):  # type: ignore[no-untyped-def]
        nonlocal staged_info, replacement_bytes
        result = real_replace(source, destination)
        if Path(destination) == path:
            replacement_bytes = path.read_bytes()
            staged_info = path.lstat()
            replacement = path.with_name("same-bytes-replacement")
            replacement.write_bytes(replacement_bytes)
            real_replace(replacement, path)
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(capture.os, "replace", replace_then_substitute)

    with pytest.raises(KeyboardInterrupt):
        transaction.apply()
    with pytest.raises(ValueError, match="^host_capture_invalid$"):
        transaction.rollback()

    assert staged_info is not None
    assert path.read_bytes() == replacement_bytes
    assert not os.path.samestat(staged_info, path.lstat())
