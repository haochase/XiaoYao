import json
from pathlib import Path

import pytest

from tools.dws_sync.runtime import TaskConfig, load_runtime, prepare_runtime
from tools.tests.test_dws_project_sync import project, write_manifest


class Protector:
    def protect(self, project_id: str, plaintext: bytes) -> bytes:
        return (project_id.encode() + b"\0" + plaintext)[::-1]

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        prefix = project_id.encode() + b"\0"
        raw = protected[::-1]
        if not raw.startswith(prefix):
            raise ValueError("private-detail")
        return raw[len(prefix):]


def inputs(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest, project())
    executable = tmp_path / "dws.exe"
    executable.write_bytes(b"test fixture")
    return manifest, executable


def test_prepare_requires_real_manifest_before_writing(tmp_path: Path) -> None:
    with pytest.raises((ValueError, OSError)):
        prepare_runtime(tmp_path, tmp_path / "absent.json", "project-1", tmp_path / "dws.exe", Protector())
    assert list(tmp_path.iterdir()) == []


def test_runtime_roundtrip_binds_config_and_protects_token(tmp_path: Path) -> None:
    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    config, project, token = load_runtime(tmp_path, Protector())
    assert project.project_id == "project-1"
    assert len(token) >= 32
    assert config.project == project.project_id
    for path in (tmp_path / ".private").rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes()
    with pytest.raises(FileExistsError):
        prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())


def test_changed_config_or_scope_cannot_reuse_credential(tmp_path: Path) -> None:
    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    path = tmp_path / ".private/qwenwork-dws-project-sync.json"
    config = json.loads(path.read_text())
    config["state"] = str(tmp_path / "other-state.json")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="runtime_binding_invalid"):
        load_runtime(tmp_path, Protector())


def test_corrupt_secret_is_sanitized(tmp_path: Path) -> None:
    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    (tmp_path / ".private/dws-runtime/credential.dpapi").write_bytes(b"bad")
    with pytest.raises(ValueError, match="runtime_credential_invalid") as caught:
        load_runtime(tmp_path, Protector())
    assert "private-detail" not in str(caught.value)


def test_task_config_rejects_extra_fields_and_c_output(tmp_path: Path) -> None:
    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    config = json.loads((tmp_path / ".private/qwenwork-dws-project-sync.json").read_text())
    with pytest.raises(ValueError):
        TaskConfig.model_validate({**config, "token": "unwanted"})
    with pytest.raises(ValueError):
        TaskConfig.model_validate({key: value for key, value in config.items() if key != "schema_version"})
    with pytest.raises(ValueError):
        TaskConfig.model_validate({**config, "source_bundle": "C:/private/bundle.json"})


def test_runtime_refuses_overlapping_outputs(tmp_path: Path) -> None:
    config = {
        "schema_version": 1, "project": "project-1", "manifest": str(tmp_path / "manifest.json"),
        "dws": str(tmp_path / "dws.exe"), "source_bundle": str(tmp_path / "out.json"),
        "context_artifact": str(tmp_path / "out.json"), "state": str(tmp_path / "state.json"),
    }
    with pytest.raises(ValueError, match="runtime_paths_overlap"):
        TaskConfig.model_validate(config)


def test_wrapper_injects_token_only_for_network_commands(tmp_path: Path, monkeypatch) -> None:
    from tools import dws_sync_runtime as wrapper
    from tools import dws_project_sync

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(dws_project_sync, "main", lambda argv, **kwargs: observed.append((argv, kwargs)) or 0)
    monkeypatch.delenv("COMPANION_DWS_SYNC_TOKEN", raising=False)
    wrapper.dispatch(tmp_path, "collect", "lease", False, Protector())
    assert "COMPANION_DWS_SYNC_TOKEN" not in observed[-1][1]["environ"]
    wrapper.dispatch(tmp_path, "push", "lease", False, Protector())
    assert observed[-1][1]["environ"]["COMPANION_DWS_SYNC_TOKEN"]
    assert "--gateway" in observed[-1][0]
    assert "127.0.0.1:8731" in " ".join(observed[-1][0])
    assert "COMPANION_DWS_SYNC_TOKEN" not in wrapper.os.environ


def test_check_missing_config_prints_no_private_details(tmp_path: Path, capsys) -> None:
    from tools.dws_sync_runtime import main

    assert main(["check"], root=tmp_path, protector=Protector()) == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "blocked", "error_type": "runtime_not_ready"}


def test_manifest_scope_change_rejects_existing_token(tmp_path: Path) -> None:
    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["projects"][0]["permission_scope"] = "project:other"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime_binding_invalid"):
        load_runtime(tmp_path, Protector())


def test_server_settings_are_scoped_and_do_not_load_device_env(tmp_path: Path, monkeypatch) -> None:
    import companion_gateway.sync_api as api
    from tools.dws_sync_runtime import build_app

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    monkeypatch.setenv("COMPANION_DB_PATH", "C:/unrelated.db")
    captured = []
    monkeypatch.setattr(api, "create_sync_app", lambda settings: captured.append(settings) or "app")
    assert build_app(tmp_path, Protector()) == "app"
    settings = captured[0]
    assert settings.database_path == tmp_path / ".private/dws-runtime/companion.db"
    assert settings.project_api_principals[0].project_ids == frozenset({"project-1"})
    assert not settings.project_api_principals[0].can_review


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_server_rejects_database_alias_before_open(tmp_path: Path, monkeypatch, suffix: str) -> None:
    import os
    import companion_gateway.sync_api as api
    from tools.dws_sync_runtime import build_app

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    target = tmp_path / ".private/dws-runtime" / ("companion.db" + suffix)
    os.link(manifest, target)
    monkeypatch.setattr(api, "create_sync_app", lambda _: pytest.fail("must not open database"))
    with pytest.raises(ValueError, match="runtime_database_invalid"):
        build_app(tmp_path, Protector())


def test_real_dpapi_runtime_roundtrip(tmp_path: Path) -> None:
    import os
    from fastapi.testclient import TestClient
    from companion_gateway.project.protection import WindowsDpapiProtector
    from tools.dws_sync_runtime import build_app

    if os.environ.get("COMPANION_RUN_DWS_RUNTIME_HOST") != "1":
        pytest.skip("explicit host DPAPI gate")
    manifest, dws = inputs(tmp_path)
    protector = WindowsDpapiProtector()
    prepare_runtime(tmp_path, manifest, "project-1", dws, protector)
    _, _, token = load_runtime(tmp_path, protector)
    assert token.encode() not in (tmp_path / ".private/dws-runtime/credential.dpapi").read_bytes()
    with TestClient(
        build_app(tmp_path, protector), base_url="http://127.0.0.1:8731",
        client=("127.0.0.1", 50000),
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/v1/projects/project-1/sync/status").status_code == 401
