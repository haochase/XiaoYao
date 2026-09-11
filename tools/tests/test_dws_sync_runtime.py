import json
from pathlib import Path
from typing import get_type_hints

import pytest

from tools.dws_sync import DwsSourceBundle, lifecycle
from tools.dws_sync.core_trust import TrustedDwsCore
from tools.dws_sync.runtime import (
    TaskConfig,
    approved_artifact_path,
    load_runtime,
    prepare_runtime,
)
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


class ExplodingProtector(Protector):
    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        pytest.fail("direct core commands must not decrypt the gateway credential")


class DirectDocumentRunner:
    def run(self, args: tuple[str, ...]) -> dict[str, object]:
        if args == ("doc", "info", "--node", "doc-1"):
            return {
                "result": {
                    "nodeId": "doc-1",
                    "contentType": "ALIDOC",
                    "extension": "adoc",
                    "title": "Direct document",
                    "shareUrl": "dingtalk://doc/doc-1",
                    "version": "v-direct",
                    "updatedAt": "2026-09-08T12:00:00+08:00",
                }
            }
        if args == ("doc", "read", "--node", "doc-1"):
            return {"data": {"markdown": "# Direct\nTrusted core content."}}
        raise AssertionError(f"unexpected DWS arguments: {args!r}")


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


def test_approved_artifact_path_is_derived_without_changing_config_dump(
    tmp_path: Path,
) -> None:
    config = TaskConfig(
        schema_version=1,
        project="project-1",
        manifest=tmp_path / "manifest.json",
        dws=tmp_path / "dws.exe",
        source_bundle=tmp_path / "source-bundle.json",
        context_artifact=tmp_path / "context-artifact.json",
        state=tmp_path / "sync-state.json",
    )
    assert approved_artifact_path(config.context_artifact) == (
        tmp_path / "context-artifact.approved.json"
    )
    assert "approved" not in config.model_dump(mode="json")


def test_runtime_refuses_path_overlapping_derived_approved_artifact(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="runtime_paths_overlap"):
        TaskConfig(
            schema_version=1,
            project="project-1",
            manifest=tmp_path / "manifest.json",
            dws=tmp_path / "dws.exe",
            source_bundle=tmp_path / "context-artifact.approved.json",
            context_artifact=tmp_path / "context-artifact.json",
            state=tmp_path / "sync-state.json",
        )


def test_wrapper_injects_token_only_for_network_commands(tmp_path: Path, monkeypatch) -> None:
    from tools import dws_sync_runtime as wrapper
    from tools import dws_project_sync

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )
    monkeypatch.delenv("COMPANION_DWS_SYNC_TOKEN", raising=False)
    wrapper.dispatch(tmp_path, "artifact", "lease", False, Protector())
    assert "COMPANION_DWS_SYNC_TOKEN" not in observed[-1][1]["environ"]
    wrapper.dispatch(tmp_path, "push", "lease", False, Protector())
    assert observed[-1][1]["environ"]["COMPANION_DWS_SYNC_TOKEN"]
    assert "--gateway" in observed[-1][0]
    assert "127.0.0.1:8731" in " ".join(observed[-1][0])
    assert "COMPANION_DWS_SYNC_TOKEN" not in wrapper.os.environ


def test_runtime_collect_is_rejected_before_any_dws_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_sync_runtime as wrapper

    monkeypatch.setattr(
        wrapper,
        "resolve_dws_launch",
        lambda *_args: pytest.fail("runtime collect must not reach DWS validation"),
    )

    with pytest.raises(SystemExit) as exited:
        wrapper.main(
            ["collect", "--run-token", "lease-token"],
            root=tmp_path,
            protector=Protector(),
        )

    assert exited.value.code == 2
    assert "collect" not in wrapper.COMMANDS


def test_check_core_never_loads_gateway_credential(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("check-core must not call load_runtime")

    monkeypatch.setattr(wrapper, "load_runtime", fail_if_called)
    monkeypatch.setattr(
        wrapper,
        "resolve_trusted_dws_core",
        lambda *_args, **_kwargs: TrustedDwsCore(
            path=dws,
            version="1.0.61",
            sha256="a" * 64,
            architecture="AMD64",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        wrapper,
        "_profile_store_present",
        lambda: True,
        raising=False,
    )

    assert wrapper.main(
        ["check-core"],
        root=tmp_path,
        protector=ExplodingProtector(),
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "core_trusted",
        "core_trusted": True,
        "profile_store_present": True,
    }


def test_collect_direct_binds_trusted_runner_to_current_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = {}
    direct_runner = object()

    class DirectRunnerFactory:
        @classmethod
        def from_official_core(cls, *args, **kwargs):
            return direct_runner

    monkeypatch.setattr(
        wrapper,
        "DwsCommandRunner",
        DirectRunnerFactory,
        raising=False,
    )

    def record_execute(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return dws_project_sync.CommandResult(0, {"status": "ok"})

    monkeypatch.setattr(dws_project_sync, "execute", record_execute)
    monkeypatch.setattr(
        wrapper,
        "load_runtime",
        lambda *_args, **_kwargs: pytest.fail(
            "collect-direct must not call load_runtime"
        ),
    )

    assert wrapper.dispatch(
        tmp_path,
        "collect-direct",
        "lease-token",
        False,
        ExplodingProtector(),
    ) == 0
    assert observed["argv"] == [
        "collect",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--dws-path",
        str(dws),
        "--output",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
    ]
    assert observed["runner"] is direct_runner
    assert observed["environ"].get("COMPANION_DWS_SYNC_TOKEN") is None


@pytest.mark.parametrize(
    "argv",
    (
        ["collect-direct"],
        ["collect-direct", "--run-token", "lease-token", "--dry-run"],
        ["collect-direct", "--run-token", "lease-token", "--unattended"],
        ["collect-direct", "--run-token", "lease-token", "--core", "dws.exe"],
        ["collect-direct", "--run-token", "lease-token", "--profile", "p"],
        ["collect-direct", "--run-token", "lease-token", "--source-id", "doc-1"],
        ["collect-direct", "--run-token", "lease-token", "--manifest", "m.json"],
        ["collect-direct", "--run-token", "lease-token", "--output", "out.json"],
        ["check-core", "--dws-path", "dws.exe"],
    ),
)
def test_direct_runtime_parser_rejects_missing_token_and_arbitrary_inputs(
    tmp_path: Path,
    argv: list[str],
) -> None:
    from tools import dws_sync_runtime as wrapper

    with pytest.raises(SystemExit) as exited:
        wrapper.main(argv, root=tmp_path, protector=ExplodingProtector())

    assert exited.value.code == 2


def test_collect_direct_success_advances_only_current_begun_lease(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    lifecycle_root = tmp_path / "lifecycle"
    monkeypatch.setattr(dws_project_sync, "LIFECYCLE_ROOT", lifecycle_root)
    monkeypatch.setattr(
        wrapper,
        "_core_runner",
        lambda *_args, **_kwargs: DirectDocumentRunner(),
    )
    started = lifecycle.begin_run("project-1", root=lifecycle_root)
    assert started.run_token is not None

    assert wrapper.dispatch(
        tmp_path,
        "collect-direct",
        started.run_token,
        False,
        ExplodingProtector(),
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "collected"
    assert output["active_sources"] == 1
    assert output["failed_sources"] == 0
    bundle_path = tmp_path / ".private/dws-runtime/source-bundle.json"
    DwsSourceBundle.model_validate_json(bundle_path.read_bytes())
    lifecycle.assert_stage(
        "project-1",
        started.run_token,
        expected="collected",
        root=lifecycle_root,
    )


def test_collect_direct_untrusted_core_keeps_begun_lease_and_credential_closed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    lifecycle_root = tmp_path / "lifecycle"
    monkeypatch.setattr(dws_project_sync, "LIFECYCLE_ROOT", lifecycle_root)
    monkeypatch.setattr(
        wrapper,
        "_core_runner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("dws_core_changed_requires_approval")
        ),
    )
    started = lifecycle.begin_run("project-1", root=lifecycle_root)
    assert started.run_token is not None

    assert wrapper.main(
        ["collect-direct", "--run-token", started.run_token],
        root=tmp_path,
        protector=ExplodingProtector(),
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "error_type": "runtime_not_ready",
    }
    lifecycle.assert_stage(
        "project-1",
        started.run_token,
        expected="begun",
        root=lifecycle_root,
    )
    lifecycle.abort_run("project-1", started.run_token, root=lifecycle_root)


def test_collect_direct_wrong_stage_never_calls_runner_or_decrypts_credential(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class NeverRun:
        def run(self, _args):
            pytest.fail("wrong-stage collect must not call DWS")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    lifecycle_root = tmp_path / "lifecycle"
    monkeypatch.setattr(dws_project_sync, "LIFECYCLE_ROOT", lifecycle_root)
    monkeypatch.setattr(wrapper, "_core_runner", lambda *_args: NeverRun())
    started = lifecycle.begin_run("project-1", root=lifecycle_root)
    assert started.run_token is not None
    lifecycle.advance_run(
        "project-1",
        started.run_token,
        expected="begun",
        target="host_info",
        root=lifecycle_root,
    )

    assert wrapper.dispatch(
        tmp_path,
        "collect-direct",
        started.run_token,
        False,
        ExplodingProtector(),
    ) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error_type": "run_stage_invalid",
    }
    lifecycle.assert_stage(
        "project-1",
        started.run_token,
        expected="host_info",
        root=lifecycle_root,
    )
    lifecycle.abort_run("project-1", started.run_token, root=lifecycle_root)


@pytest.mark.parametrize("failure", ("collect", "atomic_write"))
def test_collect_direct_failure_preserves_old_bundle_and_begun_lease(
    tmp_path: Path,
    monkeypatch,
    capsys,
    failure: str,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class FailingRunner:
        def run(self, _args):
            raise RuntimeError("private direct failure")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    lifecycle_root = tmp_path / "lifecycle"
    monkeypatch.setattr(dws_project_sync, "LIFECYCLE_ROOT", lifecycle_root)
    runner = FailingRunner() if failure == "collect" else DirectDocumentRunner()
    monkeypatch.setattr(wrapper, "_core_runner", lambda *_args: runner)
    if failure == "atomic_write":
        monkeypatch.setattr(
            dws_project_sync,
            "_atomic_write",
            lambda *_args: (_ for _ in ()).throw(
                ValueError("private_file_write_failed")
            ),
        )
    bundle_path = tmp_path / ".private/dws-runtime/source-bundle.json"
    old_bundle = b"previous-good-bundle"
    bundle_path.write_bytes(old_bundle)
    started = lifecycle.begin_run("project-1", root=lifecycle_root)
    assert started.run_token is not None

    assert wrapper.dispatch(
        tmp_path,
        "collect-direct",
        started.run_token,
        False,
        ExplodingProtector(),
    ) == 1

    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert bundle_path.read_bytes() == old_bundle
    lifecycle.assert_stage(
        "project-1",
        started.run_token,
        expected="begun",
        root=lifecycle_root,
    )
    lifecycle.abort_run("project-1", started.run_token, root=lifecycle_root)


def test_host_import_maps_fixed_paths_and_does_not_decrypt_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class HostImportProtector(Protector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            pytest.fail("host-import must not decrypt the gateway credential")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    stdin = object()
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )

    assert wrapper.dispatch(
        tmp_path,
        "host-import",
        "lease-token",
        False,
        HostImportProtector(),
        input_stream=stdin,
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "host-import",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--output",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
    ]
    assert kwargs["input_stream"] is stdin
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


@pytest.mark.parametrize(
    "command",
    (
        "capture-info",
        "complete-host-import",
    ),
)
def test_two_phase_host_commands_map_fixed_paths_without_decrypting_credential(
    tmp_path: Path,
    monkeypatch,
    command: str,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class HostImportProtector(Protector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            pytest.fail(f"{command} must not decrypt the gateway credential")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    stdin = object()
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )
    monkeypatch.setenv("COMPANION_DWS_SYNC_TOKEN", "ambient-secret")

    assert wrapper.dispatch(
        tmp_path,
        command,
        "lease-token",
        False,
        HostImportProtector(),
        input_stream=stdin,
    ) == 0

    argv, kwargs = observed[0]
    expected = [
        command,
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
    ]
    expected += [
        "--output",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
    ]
    assert argv == expected
    assert kwargs["input_stream"] is stdin
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_artifact_maps_fixed_inputs_and_does_not_decrypt_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class ArtifactProtector(Protector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            pytest.fail("artifact must not decrypt the gateway credential")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    stdin = object()
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )

    assert wrapper.dispatch(
        tmp_path,
        "artifact",
        "lease-token",
        False,
        ArtifactProtector(),
        input_stream=stdin,
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "artifact",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--sources-file",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
        "--context-file",
        str(tmp_path / ".private/dws-runtime/context-artifact.json"),
        "--state-file",
        str(tmp_path / ".private/dws-runtime/sync-state.json"),
    ]
    assert kwargs["input_stream"] is stdin
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_reuse_artifact_maps_fixed_inputs_without_decrypting_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class ReuseProtector(Protector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            pytest.fail("reuse-artifact must not decrypt the gateway credential")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )

    assert wrapper.dispatch(
        tmp_path,
        "reuse-artifact",
        "lease-token",
        False,
        ReuseProtector(),
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "reuse-artifact",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--sources-file",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
        "--context-file",
        str(tmp_path / ".private/dws-runtime/context-artifact.json"),
        "--state-file",
        str(tmp_path / ".private/dws-runtime/sync-state.json"),
    ]
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_restore_approved_maps_fixed_inputs_without_decrypting_credential(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    class RestoreProtector(Protector):
        def unprotect(self, project_id: str, protected: bytes) -> bytes:
            pytest.fail("restore-approved must not decrypt the gateway credential")

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )
    monkeypatch.setenv("COMPANION_DWS_SYNC_TOKEN", "ambient-secret")

    assert wrapper.dispatch(
        tmp_path,
        "restore-approved",
        "lease-token",
        False,
        RestoreProtector(),
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "restore-approved",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--sources-file",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
        "--context-file",
        str(tmp_path / ".private/dws-runtime/context-artifact.json"),
        "--state-file",
        str(tmp_path / ".private/dws-runtime/sync-state.json"),
    ]
    assert "input_stream" not in kwargs
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_unattended_flag_is_mapped_only_for_reuse_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )

    assert wrapper.dispatch(
        tmp_path,
        "reuse-artifact",
        "lease-token",
        False,
        Protector(),
        unattended=True,
    ) == 0
    assert observed[0][0][-1] == "--unattended"

    with pytest.raises(ValueError, match="unattended_command_invalid"):
        wrapper.dispatch(
            tmp_path,
            "push",
            "lease-token",
            False,
            Protector(),
            unattended=True,
        )


def test_runtime_parser_accepts_unattended_only_for_reuse_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_sync_runtime as wrapper

    observed = []
    monkeypatch.setattr(
        wrapper,
        "dispatch",
        lambda root, command, run_token, dry_run, protector, **kwargs: (
            observed.append((root, command, run_token, dry_run, kwargs)) or 0
        ),
    )

    assert wrapper.main(
        ["reuse-artifact", "--run-token", "lease-token", "--unattended"],
        root=tmp_path,
        protector=Protector(),
    ) == 0
    assert observed == [
        (
            tmp_path,
            "reuse-artifact",
            "lease-token",
            False,
            {"unattended": True},
        )
    ]
    with pytest.raises(SystemExit, match="2"):
        wrapper.main(
            ["push", "--run-token", "lease-token", "--unattended"],
            root=tmp_path,
            protector=Protector(),
        )


def test_recover_pending_maps_fixed_inputs_without_exposing_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )
    monkeypatch.setenv("COMPANION_DWS_SYNC_TOKEN", "ambient-secret")

    assert wrapper.dispatch(
        tmp_path,
        "recover-pending",
        None,
        False,
        Protector(),
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "recover-pending",
        "--project",
        "project-1",
        "--manifest",
        str(manifest),
        "--sources-file",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
        "--context-file",
        str(tmp_path / ".private/dws-runtime/context-artifact.json"),
        "--state-file",
        str(tmp_path / ".private/dws-runtime/sync-state.json"),
        "--database-file",
        str(tmp_path / ".private/dws-runtime/companion.db"),
    ]
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_recover_pending_runtime_parser_does_not_require_run_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_sync_runtime as wrapper

    monkeypatch.setattr(
        wrapper,
        "dispatch",
        lambda root, command, run_token, dry_run, protector: (
            0
            if (root, command, run_token, dry_run)
            == (tmp_path, "recover-pending", None, False)
            else pytest.fail("unexpected dispatch")
        ),
    )
    assert wrapper.main(
        ["recover-pending"], root=tmp_path, protector=Protector()
    ) == 0


def test_discard_rejected_pending_maps_fixed_inputs_without_exposing_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []
    monkeypatch.setattr(
        dws_project_sync,
        "execute",
        lambda argv, **kwargs: observed.append((argv, kwargs))
        or dws_project_sync.CommandResult(0, {"status": "ok"}),
    )
    monkeypatch.setenv("COMPANION_DWS_SYNC_TOKEN", "ambient-secret")

    assert wrapper.dispatch(
        tmp_path,
        "discard-rejected-pending",
        None,
        False,
        Protector(),
        confirm="sync_conflict",
    ) == 0

    argv, kwargs = observed[0]
    assert argv == [
        "discard-rejected-pending",
        "--project",
        "project-1",
        "--manifest",
        str(manifest),
        "--state-file",
        str(tmp_path / ".private/dws-runtime/sync-state.json"),
        "--database-file",
        str(tmp_path / ".private/dws-runtime/companion.db"),
        "--confirm",
        "sync_conflict",
    ]
    assert "COMPANION_DWS_SYNC_TOKEN" not in kwargs["environ"]


def test_discard_rejected_pending_runtime_parser_requires_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from tools import dws_sync_runtime as wrapper

    observed = []
    monkeypatch.setattr(
        wrapper,
        "dispatch",
        lambda root, command, run_token, dry_run, protector, **kwargs: (
            observed.append((root, command, run_token, dry_run, kwargs)) or 0
        ),
    )

    assert wrapper.main(
        ["discard-rejected-pending", "--confirm", "sync_conflict"],
        root=tmp_path,
        protector=Protector(),
    ) == 0
    assert observed == [
        (
            tmp_path,
            "discard-rejected-pending",
            None,
            False,
            {"confirm": "sync_conflict"},
        )
    ]
    with pytest.raises(SystemExit, match="2"):
        wrapper.main(
            ["discard-rejected-pending"],
            root=tmp_path,
            protector=Protector(),
        )


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


def test_dispatch_result_uses_fixed_pending_inputs_without_stdout(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    manifest, dws = inputs(tmp_path)
    prepare_runtime(tmp_path, manifest, "project-1", dws, Protector())
    observed = []

    def fake_execute(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed.append((argv, kwargs))
        return dws_project_sync.CommandResult(
            exit_code=0, payload={"status": "pending"}
        )

    monkeypatch.setattr(dws_project_sync, "execute", fake_execute)
    monkeypatch.setenv("COMPANION_DWS_SYNC_TOKEN", "ambient-secret")

    result = wrapper.dispatch_result(
        tmp_path, "pending", "lease-token", False, Protector()
    )

    assert result.exit_code == 0
    assert result.payload == {"status": "pending"}
    assert capsys.readouterr().out == ""
    argv, kwargs = observed[0]
    assert argv == [
        "pending",
        "--project",
        "project-1",
        "--run-token",
        "lease-token",
        "--manifest",
        str(manifest),
        "--sources-file",
        str(tmp_path / ".private/dws-runtime/source-bundle.json"),
        "--gateway",
        "http://127.0.0.1:8731",
    ]
    assert kwargs["environ"]["COMPANION_DWS_SYNC_TOKEN"] != "ambient-secret"


def test_dispatch_emits_dispatch_result_payload_once(tmp_path: Path, monkeypatch, capsys) -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    expected = dws_project_sync.CommandResult(
        exit_code=1, payload={"status": "error", "error_type": "sync_failed"}
    )
    monkeypatch.setattr(wrapper, "dispatch_result", lambda *_args, **_kwargs: expected)

    assert wrapper.dispatch(tmp_path, "begin", None, False, Protector()) == 1
    assert json.loads(capsys.readouterr().out) == dict(expected.payload)


def test_dispatch_result_return_annotation_resolves_at_runtime() -> None:
    from tools import dws_project_sync
    from tools import dws_sync_runtime as wrapper

    assert get_type_hints(wrapper.dispatch_result)["return"] is (
        dws_project_sync.CommandResult
    )


def test_run_once_runtime_emits_one_sanitized_json_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from tools import dws_sync_runtime as wrapper

    class FakeResult:
        def to_dict(self) -> dict[str, object]:
            return {
                "status": "completed",
                "stage": "end",
                "project_id": "project-1",
                "outcome": "applied",
                "project_status": "healthy",
                "active_sources": 1,
                "failed_sources": 0,
                "accepted_sources": 1,
                "error_type": None,
                "manual_refresh_required": False,
                "rerun_count": 0,
            }

    observed = []
    monkeypatch.setattr(
        wrapper,
        "run_once",
        lambda root, protector: observed.append((root, protector)) or FakeResult(),
        raising=False,
    )

    assert wrapper.main(["run-once"], root=tmp_path, protector=Protector()) == 0
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["status"] == "completed"
    assert "token" not in output
    assert len(observed) == 1
    assert observed[0][0] == tmp_path
    assert isinstance(observed[0][1], Protector)


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    (
        ("completed", 0),
        ("coalesced", 0),
        ("awaiting_artifact", 2),
        ("failed", 1),
    ),
)
def test_run_once_runtime_uses_status_specific_exit_codes(
    tmp_path: Path,
    monkeypatch,
    capsys,
    status: str,
    expected_exit: int,
) -> None:
    from tools import dws_sync_runtime as wrapper

    class FakeResult:
        def to_dict(self) -> dict[str, object]:
            return {"status": status, "manual_refresh_required": status == "awaiting_artifact"}

    monkeypatch.setattr(
        wrapper, "run_once", lambda *_args: FakeResult(), raising=False
    )

    assert wrapper.main(["run-once"], root=tmp_path, protector=Protector()) == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    "argv",
    (
        ["run-once", "--project", "project-1"],
        ["run-once", "--run-token", "private-token"],
        ["run-once", "--profile", "private-profile"],
        ["run-once", "--url", "http://127.0.0.1:8731"],
    ),
)
def test_run_once_runtime_rejects_all_user_supplied_execution_inputs(
    tmp_path: Path,
    argv: list[str],
) -> None:
    from tools import dws_sync_runtime as wrapper

    with pytest.raises(SystemExit, match="2"):
        wrapper.main(argv, root=tmp_path, protector=Protector())


@pytest.mark.parametrize(
    ("command", "method", "status", "expected_exit"),
    (
        ("session-start", "start_session", "active", 0),
        ("session-start", "start_session", "failed", 1),
        ("session-tick", "tick_session", "active", 0),
        ("session-status", "session_status", "attention_required", 2),
        ("session-stop", "stop_session", "stopped", 0),
        ("session-resume", "resume_session", "failed", 1),
    ),
)
def test_session_runtime_commands_emit_one_sanitized_json_and_map_exit_codes(
    tmp_path: Path,
    monkeypatch,
    capsys,
    command: str,
    method: str,
    status: str,
    expected_exit: int,
) -> None:
    from tools import dws_sync_runtime as wrapper

    observed: list[tuple[Path, Protector]] = []

    class FakeResult:
        def to_dict(self) -> dict[str, object]:
            return {
                "status": status,
                "started_at": "2026-09-11T09:00:00+00:00",
                "expires_at": "2026-09-11T11:00:00+00:00",
                "next_due_at": None,
                "stage": "push",
                "error_type": "sync_failed" if status == "failed" else None,
                "release_status": "aborted",
                "token": "must-not-print",
            }

    def fake(root: Path, protector: Protector | None = None) -> FakeResult:
        if protector is not None:
            observed.append((root, protector))
        return FakeResult()

    monkeypatch.setattr(wrapper.session, method, fake)

    assert (
        wrapper.main([command], root=tmp_path, protector=Protector())
        == expected_exit
    )
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    payload = json.loads(output)
    assert payload["status"] == status
    assert "token" not in payload
    if method in {"start_session", "tick_session"}:
        assert observed == [(tmp_path, observed[0][1])]


@pytest.mark.parametrize(
    "argv",
    (
        ["session-start", "--project", "project-1"],
        ["session-tick", "--profile", "private-profile"],
        ["session-status", "--url", "http://127.0.0.1:8731"],
        ["session-stop", "--run-token", "private-token"],
        ["session-resume", "--project", "project-1"],
    ),
)
def test_session_runtime_rejects_all_user_supplied_execution_inputs(
    tmp_path: Path,
    argv: list[str],
    capsys,
) -> None:
    from tools import dws_sync_runtime as wrapper

    assert wrapper.main(argv, root=tmp_path, protector=Protector()) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == {
        "status": "failed",
        "error_type": "session_arguments_invalid",
    }
    assert all(value not in captured.out for value in argv[1:])
