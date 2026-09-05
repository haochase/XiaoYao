import json

import pytest

from tools.dws_project_sync import main
from tools.dws_sync.runner import DwsCommandRunner, DwsReadError
from tools.tests.test_dws_project_sync import project, write_manifest
from tools.tests.test_dws_sync_adapters import FakeProcess, RecordingPopen


NOTICE = b"[dws-bash:pending-post-tool-use]:private-id DWS command is pending host-side execution"


def runner(tmp_path, data):
    executable = tmp_path / "dws.exe"
    executable.write_bytes(b"fixture")
    return DwsCommandRunner(
        executable, profile="private-profile", popen=RecordingPopen(FakeProcess(data))
    )


def test_handoff_is_actionable_not_a_source_read_failure(tmp_path):
    with pytest.raises(ValueError, match="^host_handoff_required$") as caught:
        runner(tmp_path, NOTICE).run(("doc", "info", "--node", "doc-1"))
    assert "private" not in str(caught.value)


def test_notice_inside_business_json_is_not_a_handoff(tmp_path):
    data = {"markdown": NOTICE.decode()}
    assert runner(tmp_path, json.dumps(data).encode()).run(("doc", "read")) == data


def test_other_invalid_output_remains_invalid_payload(tmp_path):
    with pytest.raises(DwsReadError) as caught:
        runner(tmp_path, b"other invalid output").run(("doc", "info"))
    assert caught.value.error_type.value == "invalid_payload"


def test_collect_handoff_preserves_bundle_and_reports_safe_error(tmp_path, capsys, monkeypatch):
    import tools.dws_project_sync as cli

    monkeypatch.setattr(cli, "LIFECYCLE_ROOT", tmp_path / "locks")
    manifest, output = tmp_path / "manifest.json", tmp_path / "sources.json"
    write_manifest(manifest, project())
    output.write_bytes(b"existing")
    selected_runner = runner(tmp_path, NOTICE)
    result = main([
        "collect", "--manifest", str(manifest), "--project", "project-1",
        "--dws-path", str(tmp_path / "dws.exe"), "--output", str(output),
    ], runner=selected_runner)
    assert result == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error", "error_type": "host_handoff_required",
    }
    assert output.read_bytes() == b"existing"
