from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from tools.dws_sync.launch import resolve_dws_launch
from tools.dws_sync.runtime import load_runtime, prepare_runtime, runtime_database


COMMANDS = ("begin", "collect", "pending", "artifact", "push", "end", "abort")


def dispatch(
    root: Path,
    command: str,
    run_token: str | None,
    dry_run: bool,
    protector: ContentProtector,
) -> int:
    from tools import dws_project_sync as cli

    config, _project, token = load_runtime(root, protector)
    argv = [command, "--project", config.project]
    if run_token:
        argv += ["--run-token", run_token]
    if command in {"collect", "pending", "push"}:
        argv += ["--manifest", str(config.manifest)]
    if command == "collect":
        resolve_dws_launch(config.dws)
        argv += ["--dws-path", str(config.dws), "--output", str(config.source_bundle)]
    if command in {"pending", "push"}:
        argv += [
            "--sources-file", str(config.source_bundle),
            "--gateway", "http://127.0.0.1:8731",
        ]
    if command in {"artifact", "push"}:
        argv += ["--context-file", str(config.context_artifact)]
    if command == "push":
        argv += ["--state-file", str(config.state)]
        if dry_run:
            argv += ["--dry-run"]
    environment = dict(os.environ)
    environment.pop("COMPANION_DWS_SYNC_TOKEN", None)
    if command in {"pending", "push"} and not dry_run:
        environment["COMPANION_DWS_SYNC_TOKEN"] = token
    return cli.main(argv, environ=environment)


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
    commands.add_parser("serve")
    for command in COMMANDS:
        sub = commands.add_parser(command)
        if command != "begin":
            sub.add_argument("--run-token", required=True)
        if command == "push":
            sub.add_argument("--dry-run", action="store_true")
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
        elif args.command == "serve":
            import uvicorn

            uvicorn.run(
                build_app(root, selected_protector),
                host="127.0.0.1", port=8731, proxy_headers=False, access_log=False,
            )
            return 0
        else:
            return dispatch(
                root, args.command, getattr(args, "run_token", None),
                getattr(args, "dry_run", False), selected_protector,
            )
    except Exception:
        print(json.dumps({"status": "blocked", "error_type": "runtime_not_ready"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
