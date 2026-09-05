from __future__ import annotations

import argparse
import json
from pathlib import Path

from gateway_runner_common import _prepare_import_paths


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the XiaoYao gateway")
    parser.add_argument("--gateway-root", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8723)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    status = _prepare_import_paths(args.gateway_root)
    import companion_gateway
    import lark_channel
    import uvicorn

    if args.check:
        print(json.dumps({"status": "ready", **status}, separators=(",", ":")))
        return 0
    uvicorn.run(
        "companion_gateway.api:create_default_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
