import base64
import json
from datetime import UTC, datetime

import pytest

from companion_gateway.project.sync_models import SourceErrorType
from tools.dws_sync.host_bridge import import_single_document_bundle
from tools.dws_sync.manifest import DwsProjectManifest, DwsSourceSpec
from tools.dws_sync.runner import DwsReadError


NOW = datetime(2026, 9, 6, 8, 30, tzinfo=UTC)


def project(
    *,
    project_id: str = "project-1",
    sources: tuple[DwsSourceSpec, ...] | None = None,
) -> DwsProjectManifest:
    return DwsProjectManifest(
        project_id=project_id,
        project_name="测试项目",
        profile="private-profile",
        permission_scope="project:project-1",
        sources=sources
        or (DwsSourceSpec(source_type="document", source_id="doc-1"),),
    )


def result(operation: str, payload: object) -> dict[str, object]:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "operation": operation,
        "encoding": "base64-json",
        "byte_count": len(raw),
        "payload": base64.b64encode(raw).decode("ascii"),
    }


def host_input(
    *,
    project_id: str = "project-1",
    info: object | None = None,
    read: object | None = None,
) -> bytes:
    info = info or {
        "result": {
            "nodeId": "doc-1",
            "contentType": "ALIDOC",
            "extension": "adoc",
            "title": "设计说明",
            "shareUrl": "dingtalk://document/doc-1",
            "version": "v7",
            "updatedAt": "2026-09-06T16:30:00+08:00",
        }
    }
    read = read or {"data": {"markdown": "# 设计说明\n中文、\"引号\"与换行\n保持原样。"}}
    return json.dumps(
        {
            "schema_version": 1,
            "project_id": project_id,
            "results": [result("doc_info", info), result("doc_read", read)],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_import_single_document_bundle_replays_exact_document_calls() -> None:
    raw = host_input()

    bundle = import_single_document_bundle(raw, project(), collected_at=NOW)

    assert bundle.project_id == "project-1"
    assert bundle.collected_at == NOW
    assert len(bundle.records) == 1
    record = bundle.records[0]
    assert record.status == "active"
    assert record.source_id == "doc-1"
    assert record.content_text == "# 设计说明\n中文、\"引号\"与换行\n保持原样。"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(project_id="other-project"),
        lambda value: value["results"].reverse(),
        lambda value: value.update(results=value["results"][:1]),
        lambda value: value["results"].append(value["results"][0]),
        lambda value: value["results"][1].update(operation="doc_info"),
        lambda value: value["results"][0].update(extra="forbidden"),
        lambda value: value.update(extra="forbidden"),
        lambda value: value["results"][0].update(encoding="utf-8"),
        lambda value: value["results"][0].update(payload="not-base64!"),
        lambda value: value["results"][0].update(byte_count=1),
    ],
)
def test_import_rejects_invalid_outer_contract(mutate) -> None:
    value = json.loads(host_input())
    mutate(value)

    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(
            json.dumps(value).encode(), project(), collected_at=NOW
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1,"project_id":"project-1","results":[]}',
        b'{"schema_version":NaN,"project_id":"project-1","results":[]}',
        b"\xff",
        b"[]",
    ],
)
def test_import_rejects_duplicate_keys_non_finite_bad_utf8_and_non_object(
    raw: bytes,
) -> None:
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(raw, project(), collected_at=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        b'[]',
        b'{"value":NaN}',
        b'{"value":1,"value":2}',
        b"\xff",
        b"[dws-bash:pending-post-tool-use]:private",
    ],
)
def test_import_rejects_invalid_or_placeholder_decoded_payload(
    payload: bytes,
) -> None:
    value = json.loads(host_input())
    value["results"][0].update(
        byte_count=len(payload),
        payload=base64.b64encode(payload).decode("ascii"),
    )

    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(
            json.dumps(value).encode(), project(), collected_at=NOW
        )


def test_import_rejects_oversized_item_and_total_input() -> None:
    oversized = b"x" * (2_097_152 + 1)
    value = json.loads(host_input())
    value["results"][0].update(
        byte_count=len(oversized),
        payload=base64.b64encode(oversized).decode("ascii"),
    )
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(
            json.dumps(value).encode(), project(), collected_at=NOW
        )


def test_import_rejects_decoded_total_over_limit_with_valid_item_sizes() -> None:
    padding = "x" * 1_048_570
    results = [
        result("doc_info", {"padding": padding}),
        result("doc_read", {"padding": padding}),
    ]
    assert all(item["byte_count"] <= 2_097_152 for item in results)
    assert sum(item["byte_count"] for item in results) > 2_097_152
    raw = json.dumps(
        {
            "schema_version": 1,
            "project_id": "project-1",
            "results": results,
        },
        separators=(",", ":"),
    ).encode()

    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(raw, project(), collected_at=NOW)
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(
            host_input() + b" " * 5_600_000,
            project(),
            collected_at=NOW,
        )


def test_import_rejects_wrong_source_shape_and_adapter_failure() -> None:
    invalid_projects = (
        project(
            sources=(DwsSourceSpec(source_type="task", source_id="doc-1"),)
        ),
        project(
            sources=(
                DwsSourceSpec(source_type="document", source_id="doc-1"),
                DwsSourceSpec(source_type="document", source_id="doc-2"),
            )
        ),
    )
    for selected in invalid_projects:
        with pytest.raises(ValueError, match="^host_import_invalid$"):
            import_single_document_bundle(
                host_input(), selected, collected_at=NOW
            )

    with pytest.raises(DwsReadError) as caught:
        import_single_document_bundle(
            host_input(info={"nodeId": "wrong"}),
            project(),
            collected_at=NOW,
        )
    assert caught.value.error_type is SourceErrorType.INVALID_PAYLOAD


def test_import_rejects_naive_collection_time() -> None:
    with pytest.raises(ValueError, match="^host_import_invalid$"):
        import_single_document_bundle(
            host_input(),
            project(),
            collected_at=datetime(2026, 9, 6, 8, 30),
        )
