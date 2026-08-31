from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the XiaoYao gateway")
    parser.add_argument("--gateway-root", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8723)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def _prepare_import_paths(gateway_root: Path) -> dict[str, object]:
    resolved_gateway = gateway_root.resolve(strict=True)
    source_directory = resolved_gateway / "src"
    if not source_directory.is_dir():
        raise ValueError(f"gateway source directory was not found: {source_directory}")

    project_root = Path(__file__).resolve().parents[1]
    vendor_site_packages = project_root / ".vendor" / "python-site"
    paths = [source_directory]
    if vendor_site_packages.is_dir():
        paths.append(vendor_site_packages)
    for path in reversed(paths):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    os.chdir(resolved_gateway)
    return {
        "gateway_root": str(resolved_gateway),
        "source_available": source_directory.is_dir(),
        "vendor_available": vendor_site_packages.is_dir(),
    }


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
