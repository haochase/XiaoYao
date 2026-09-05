import io
import json

import pytest

from tools.dws_stdout_check import capture, summarize_bytes


@pytest.mark.parametrize("raw,kind", [
    (b"", "empty"), (b"  \n", "empty"),
    (b'{"secret":"do-not-print"}', "object"),
    (b'["do-not-print"]', "array"),
    (b'"do-not-print"', "string"),
    (b'NOTICE\n{"secret":"do-not-print"}', "invalid"),
    (b'{}\n{}', "invalid"), (b'\xff\xfe' + '{}'.encode('utf-16-le'), "object"),
])
def test_report_only_contains_structural_facts(raw, kind):
    result = summarize_bytes(raw)
    assert result["json_kind"] == kind
    assert result["bytes"] == len(raw)
    assert "do-not-print" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_classifies_bom_and_ansi_without_repairing_input():
    assert summarize_bytes(b'\xef\xbb\xbf{}')["bom"] == "utf8"
    result = summarize_bytes(b'\x1b[32m{}\x1b[0m')
    assert result["ansi_present"]
    assert result["json_kind"] == "invalid"


def test_capture_closes_both_pipes_and_preserves_argument_array():
    seen = []

    class Process:
        stdout = io.BytesIO(b'plain notice')
        stderr = io.BytesIO(b'{"secret":"do-not-print"}')

        def wait(self, timeout):
            return 0

    process = Process()

    def popen(command, **kwargs):
        seen.append((command, kwargs))
        return process

    result = capture(["fixed.exe", "doc", "info"], {}, popen=popen)
    assert result["returncode"] == 0
    assert result["stdout"]["json_kind"] == "invalid"
    assert result["stderr"]["json_kind"] == "object"
    assert process.stdout.closed and process.stderr.closed
    assert seen[0][0] == ["fixed.exe", "doc", "info"]
    assert seen[0][1]["shell"] is False
    assert "do-not-print" not in json.dumps(result)


def test_missing_session_stops_before_private_config_read(monkeypatch, capsys):
    import tools.dws_stdout_check as diagnostic

    monkeypatch.delenv("QODERWORK_SOURCE_CHAT_ID", raising=False)
    monkeypatch.setattr(diagnostic, "read_object", lambda _: pytest.fail("must not read"))
    assert diagnostic.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked", "error_type": "qwen_session_required",
    }


def test_capture_rejects_oversized_stderr_without_printing():
    from tools.dws_sync.runner import MAX_DWS_STDOUT_BYTES

    class Process:
        stdout = io.BytesIO(b'{}')
        stderr = io.BytesIO(b'x' * (MAX_DWS_STDOUT_BYTES + 1))

        def wait(self, timeout):
            return 0

    process = Process()
    with pytest.raises(ValueError, match="diagnostic_stderr_unavailable"):
        capture(["fixed.exe"], {}, popen=lambda *args, **kwargs: process)
    assert process.stdout.closed and process.stderr.closed
