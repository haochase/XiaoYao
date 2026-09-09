from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "gateway" / "src"))
    sys.path.insert(0, str(ROOT))

from companion_gateway.project.auth import ProjectApiPrincipal
from companion_gateway.project.protection import ContentProtector, WindowsDpapiProtector
from companion_gateway.settings import Settings
from tools.dws_project_sync import CommandResult
from tools.dws_sync.core_trust import resolve_trusted_dws_core
from tools.dws_sync.launch import resolve_dws_launch
from tools.dws_sync.manifest import DwsManifest, DwsProjectManifest
from tools.dws_sync.runner import DwsCommandRunner
from tools.dws_sync.runtime import (
    CONFIG_NAME,
    RUNTIME_NAME,
    TaskConfig,
    load_runtime,
    prepare_runtime,
    read_object,
    require_local,
    runtime_database,
)


COMMANDS = (
    "begin",
    "collect-direct",
    "capture-info",
    "host-import",
    "complete-host-import",
    "pending",
    "discard-rejected-pending",
    "recover-pending",
    "reuse-artifact",
    "restore-approved",
    "artifact",
    "push",
    "end",
    "abort",
)


def _profile_store_present() -> bool:
    profile_store = Path.home() / ".dws"
    try:
        info = profile_store.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & 1024
    )


def _load_host_import_config(
    root: Path,
) -> tuple[TaskConfig, DwsProjectManifest]:
    require_local(root)
    config_path = root / CONFIG_NAME
    config = TaskConfig.model_validate(read_object(config_path))
    for path in (
        config.manifest,
        config.dws,
        config.source_bundle,
        config.context_artifact,
        config.state,
    ):
        if path.resolve() == config_path.resolve() or (
            path.exists() and path.samefile(config_path)
        ):
            raise ValueError("runtime_paths_overlap")
        if not path.parent.is_dir():
            raise ValueError("runtime_parent_missing")
    matches = [
        project
        for project in DwsManifest.load(config.manifest).projects
        if project.project_id == config.project
    ]
    if len(matches) != 1:
        raise ValueError("runtime_project_missing")
    return config, matches[0]


def _core_runner(
    root: Path,
    config: TaskConfig,
    project: DwsProjectManifest,
) -> DwsCommandRunner:
    return DwsCommandRunner.from_official_core(
        config.dws,
        runtime_root=root / RUNTIME_NAME,
        profile=project.profile,
    )


def dispatch_result(
    root: Path,
    command: str,
    run_token: str | None,
    dry_run: bool,
    protector: ContentProtector,
    *,
    input_stream: object | None = None,
    unattended: bool = False,
    confirm: str | None = None,
) -> CommandResult:
    from tools import dws_project_sync as cli

    if command not in COMMANDS:
        raise ValueError("runtime_command_invalid")
    if unattended and command != "reuse-artifact":
        raise ValueError("unattended_command_invalid")
    if confirm is not None and command != "discard-rejected-pending":
        raise ValueError("confirmation_command_invalid")
    if command == "collect-direct":
        config, project = _load_host_import_config(root)
        runner = _core_runner(root, config, project)
        argv = [
            "collect",
            "--project",
            config.project,
            "--run-token",
            run_token,
            "--manifest",
            str(config.manifest),
            "--dws-path",
            str(config.dws),
            "--output",
            str(config.source_bundle),
        ]
        environment = dict(os.environ)
        environment.pop("COMPANION_DWS_SYNC_TOKEN", None)
        return cli.execute(
            argv,
            runner=runner,
            environ=environment,
            direct_collection=True,
        )
    if command in {
        "artifact",
        "capture-info",
        "host-import",
        "complete-host-import",
        "reuse-artifact",
        "restore-approved",
        "discard-rejected-pending",
    }:
        config, _project = _load_host_import_config(root)
        token = None
    else:
        config, _project, token = load_runtime(root, protector)
    argv = [command, "--project", config.project]
    if run_token:
        argv += ["--run-token", run_token]
    if command in {
        "artifact",
        "capture-info",
        "host-import",
        "complete-host-import",
        "pending",
        "push",
        "discard-rejected-pending",
        "recover-pending",
        "reuse-artifact",
        "restore-approved",
    }:
        argv += ["--manifest", str(config.manifest)]
    if command in {
        "capture-info",
        "host-import",
        "complete-host-import",
    }:
        argv += ["--output", str(config.source_bundle)]
    if command in {
        "artifact",
        "pending",
        "push",
        "recover-pending",
        "reuse-artifact",
        "restore-approved",
    }:
        argv += ["--sources-file", str(config.source_bundle)]
    if command in {"pending", "push"}:
        argv += [
            "--gateway", "http://127.0.0.1:8731",
        ]
    if command in {
        "artifact",
        "push",
        "recover-pending",
        "reuse-artifact",
        "restore-approved",
    }:
        argv += ["--context-file", str(config.context_artifact)]
    if command in {
        "artifact",
        "discard-rejected-pending",
        "push",
        "recover-pending",
        "reuse-artifact",
        "restore-approved",
    }:
        argv += ["--state-file", str(config.state)]
    if command == "recover-pending":
        argv += ["--database-file", str(runtime_database(root))]
    if command == "discard-rejected-pending":
        argv += [
            "--database-file",
            str(runtime_database(root)),
            "--confirm",
            "" if confirm is None else confirm,
        ]
    if command == "push":
        if dry_run:
            argv += ["--dry-run"]
    if command == "reuse-artifact" and unattended:
        argv += ["--unattended"]
    environment = dict(os.environ)
    environment.pop("COMPANION_DWS_SYNC_TOKEN", None)
    if command in {"pending", "push"} and not dry_run:
        environment["COMPANION_DWS_SYNC_TOKEN"] = token
    kwargs: dict[str, object] = {"environ": environment}
    if command in {
        "artifact",
        "capture-info",
        "host-import",
        "complete-host-import",
    }:
        kwargs["input_stream"] = (
            sys.stdin.buffer if input_stream is None else input_stream
        )
    return cli.execute(argv, **kwargs)


def dispatch(
    root: Path,
    command: str,
    run_token: str | None,
    dry_run: bool,
    protector: ContentProtector,
    *,
    input_stream: object | None = None,
    unattended: bool = False,
    confirm: str | None = None,
) -> int:
    from tools import dws_project_sync as cli

    result = dispatch_result(
        root,
        command,
        run_token,
        dry_run,
        protector,
        input_stream=input_stream,
        unattended=unattended,
        confirm=confirm,
    )
    cli._emit(result.payload)
    return result.exit_code


def build_app(root: Path, protector: ContentProtector):
    from companion_gateway.sync_api import create_sync_app

    config, project, token = load_runtime(root, protector)
    settings = Settings(
        database_path=runtime_database(root),
        project_api_principals=(
            ProjectApiPrincipal(
                principal_id="qwenwork-sync",
                token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                project_ids=frozenset({config.project}),
                permission_scopes=frozenset({project.permission_scope}),
            ),
        ),
    )
    return create_sync_app(settings)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    protector: ContentProtector | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Private local DWS sync runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--dws", type=Path, required=True)
    commands.add_parser("check")
    commands.add_parser("check-core")
    commands.add_parser("serve")
    for command in COMMANDS:
        sub = commands.add_parser(command)
        if command not in {
            "begin",
            "discard-rejected-pending",
            "recover-pending",
        }:
            sub.add_argument("--run-token", required=True)
        if command == "discard-rejected-pending":
            sub.add_argument("--confirm", required=True)
        if command == "push":
            sub.add_argument("--dry-run", action="store_true")
        if command == "reuse-artifact":
            sub.add_argument("--unattended", action="store_true")
    args = parser.parse_args(argv)
    try:
        selected_protector = protector or WindowsDpapiProtector()
        if args.command == "prepare":
            resolve_dws_launch(args.dws)
            prepare_runtime(
                root, args.manifest, args.project, args.dws, selected_protector
            )
            result = {"status": "prepared", "credential_protected": True}
        elif args.command == "check":
            config, project, _token = load_runtime(root, selected_protector)
            resolve_dws_launch(config.dws)
            result = {
                "status": "configured",
                "source_count": len(project.sources),
                "credential_protected": True,
                "qwen_session_present": bool(os.environ.get("QODERWORK_SOURCE_CHAT_ID")),
                "skill_registration": "not_checked",
                "gateway_listening": "not_checked",
            }
        elif args.command == "check-core":
            config, _project = _load_host_import_config(root)
            resolve_trusted_dws_core(config.dws)
            result = {
                "status": "core_trusted",
                "core_trusted": True,
                "profile_store_present": _profile_store_present(),
            }
        elif args.command == "serve":
            import uvicorn

            uvicorn.run(
                build_app(root, selected_protector),
                host="127.0.0.1", port=8731, proxy_headers=False, access_log=False,
            )
            return 0
        else:
            dispatch_options = {}
            if getattr(args, "unattended", False):
                dispatch_options["unattended"] = True
            if args.command == "discard-rejected-pending":
                dispatch_options["confirm"] = args.confirm
            return dispatch(
                root, args.command, getattr(args, "run_token", None),
                getattr(args, "dry_run", False), selected_protector,
                **dispatch_options,
            )
    except Exception:
        print(json.dumps({"status": "blocked", "error_type": "runtime_not_ready"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
