from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.dws_sync.launch import _OFFICIAL_WRAPPER


def official_installation(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    installation = tmp_path / "bin"
    extension = installation / "ext"
    extension.mkdir(parents=True)
    wrapper = installation / "dws"
    wrapper.write_text(_OFFICIAL_WRAPPER, encoding="utf-8", newline="\n")
    (extension / "cli-common-shim-windows-amd64.exe").write_bytes(b"shim")
    (extension / ".dws-version").write_text("1.0.61\n", encoding="ascii")
    core = extension / "dws-core-windows-amd64.exe"
    core.write_bytes(b"signed-core-fixture")
    return installation, wrapper, core, tmp_path / "approved_dws_cores.json"


def write_approval(path: Path, core: Path, *, version: str = "1.0.61") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cores": [
                    {
                        "architecture": "AMD64",
                        "core_version": version,
                        "core_sha256": hashlib.sha256(core.read_bytes()).hexdigest(),
                        "signer_thumbprint": "D" * 40,
                        "publisher_name": "BRIGHT ZENITH PRIVATE LIMITED",
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
