from __future__ import annotations

import argparse
import json
from pathlib import Path


def _text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in {".git", "build"} for part in path.parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            yield path, path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def probe_source(source_root: Path) -> dict[str, bool | str]:
    source = Path(source_root)
    if not source.is_dir():
        return {
            "status": "error",
            "source_present": False,
            "board_camera_definition": False,
            "ov2640_configured": False,
            "psram_configured": False,
        }

    board_root = source / "main" / "boards" / "waveshare" / "esp32-s3-audio-board"
    board_camera_definition = False
    ov2640_configured = False
    psram_configured = False
    for path, content in _text_files(source):
        normalized_name = path.name.casefold()
        in_board = board_root in path.parents or path == board_root
        if in_board and ("camera" in normalized_name or "ov2640" in content.casefold()):
            board_camera_definition = True
        upper = content.upper()
        if "CONFIG_CAMERA_OV2640=Y" in upper or "OV2640" in upper:
            ov2640_configured = True
        if "CONFIG_SPIRAM=Y" in upper or "CONFIG_SPIRAM_SUPPORT=Y" in upper:
            psram_configured = True
    ready = board_camera_definition and ov2640_configured and psram_configured
    return {
        "status": "ok" if ready else "error",
        "source_present": True,
        "board_camera_definition": board_camera_definition,
        "ov2640_configured": ov2640_configured,
        "psram_configured": psram_configured,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only OV2640 profile probe.")
    parser.add_argument("source_root", type=Path)
    result = probe_source(parser.parse_args().source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
