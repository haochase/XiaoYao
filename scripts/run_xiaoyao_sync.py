from __future__ import annotations

import argparse
import json
from pathlib import Path

from gateway_runner_common import _prepare_import_paths


_SYNC_HOST = "127.0.0.1"
_SYNC_PORT = 8731


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the XiaoYao sync gateway")
    parser.add_argument("--gateway-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.host != _SYNC_HOST:
        parser.error("--host must be exactly 127.0.0.1")
    if args.port != _SYNC_PORT:
        parser.error("--port must be exactly 8731")
    return args


def main() -> int:
    args = _parse_args()
    status = _prepare_import_paths(args.gateway_root)
    from companion_gateway import sync_api
    import uvicorn

    if args.check:
        if not callable(sync_api.create_default_sync_app):
            raise RuntimeError("sync application factory is unavailable")
        print(
            json.dumps(
                {
                    "status": "ready",
                    "host": args.host,
                    "port": args.port,
                    **status,
                },
                separators=(",", ":"),
            )
        )
        return 0
    uvicorn.run(
        "companion_gateway.sync_api:create_default_sync_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
