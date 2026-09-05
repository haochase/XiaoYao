import base64
import json

import pytest

from tools.dws_host_probe import probe


def envelope(value):
    payload = json.dumps(value, ensure_ascii=False).encode()
    return json.dumps({
        "encoding": "base64-json", "byte_count": len(payload),
        "payload": base64.b64encode(payload).decode(),
    }).encode()


def test_probe_reads_metadata_without_exposing_it():
    result = probe(envelope({
        "success": True, "nodeId": "private-id", "contentType": "ALIDOC",
        "extension": "adoc", "name": "private-title", "docUrl": "private-url",
    }), "doc_info", "private-id")
    assert result["identity_matches"]
    assert result["document_contract_matches"]
    assert "private" not in json.dumps(result)


def test_probe_uses_adapter_type_fallback_for_metadata():
    result = probe(envelope({
        "nodeId": "private-id", "contentType": "ALIDOC", "type": "adoc",
    }), "doc_info", "private-id")
    assert result["document_contract_matches"]


def test_probe_uses_adapter_identity_alias_priority():
    result = probe(envelope({
        "source_id": "wrong-id", "nodeId": "private-id",
        "contentType": "ALIDOC", "extension": "adoc",
    }), "doc_info", "private-id")
    assert result["identity_present"]
    assert not result["identity_matches"]
    assert not result["document_contract_matches"]


def test_probe_preserves_chinese_newlines_and_quotes():
    value = '中文\n"quoted"\\literal'
    result = probe(envelope({"markdown": value}), "doc_read", "private-id")
    assert result["markdown_chars"] == len(value)
    assert result["document_contract_matches"]
    assert "quoted" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    {"encoding": "unknown"}, {"byte_count": 999}, {"byte_count": True},
    {"payload": "%%%"}, {"extra": "private"},
])
def test_probe_rejects_malformed_transport(change):
    data = json.loads(envelope({}))
    data.update(change)
    with pytest.raises(ValueError):
        probe(json.dumps(data).encode(), "doc_info", "private-id")


@pytest.mark.parametrize("raw", [b'[]', b'{"x":1,"x":2}', b'{"x":NaN}',
                                  b'[dws-bash:pending-post-tool-use]:private'])
def test_probe_rejects_non_business_payload(raw):
    data = {"encoding": "base64-json", "byte_count": len(raw),
            "payload": base64.b64encode(raw).decode()}
    with pytest.raises(ValueError):
        probe(json.dumps(data).encode(), "doc_info", "private-id")


def test_probe_marks_unverified_body_shape_not_supported():
    result = probe(envelope({"content": "private-body"}), "doc_read", "private-id")
    assert not result["document_contract_matches"]
    assert result["content_kind"] == "string"
    assert "private" not in json.dumps(result)
