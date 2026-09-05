from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_import_paths(gateway_root: Path) -> dict[str, object]:
    resolved_gateway = gateway_root.resolve(strict=True)
    source_directory = resolved_gateway / "src"
    if not source_directory.is_dir():
        raise ValueError(
            f"gateway source directory was not found: {source_directory}"
        )

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
