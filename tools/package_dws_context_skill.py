from __future__ import annotations

import argparse
import io
import json
import stat
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SKILL_NAME = "hui-anchor-dws-project-context-v1"
SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / SKILL_NAME
FILES = ("SKILL.md", ".skill-metadata.yaml", "contract.md")


def package_skill(output: Path) -> None:
    if not output.is_absolute() or output.drive.upper() != "E:":
        raise ValueError("output_requires_e_drive")
    for parent in (output.parent, *output.parent.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("output_parent_invalid")
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name in FILES:
            path = SKILL_ROOT / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise ValueError("skill_file_invalid")
            data = path.read_bytes()
            if len(data) > 65536:
                raise ValueError("skill_file_too_large")
            entry = ZipInfo(f"{SKILL_NAME}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    with output.open("xb") as stream:
        stream.write(buffer.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the public QwenWork context skill")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package_skill(args.output)
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "error_type": "skill_package_failed"}))
        return 1
    print(json.dumps({"status": "packaged", "file_count": len(FILES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
