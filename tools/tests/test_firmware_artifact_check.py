import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-xiaoyao-firmware-artifact.ps1"


def run_check(image_path: Path, expected_sha256: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-File",
        str(SCRIPT),
        "-ImagePath",
        str(image_path),
    ]
    if expected_sha256 is not None:
        command.extend(["-ExpectedSha256", expected_sha256])

    return subprocess.run(command, check=False, capture_output=True, text=True)


def parse_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(completed.stdout)


def test_artifact_check_reports_sha256_for_a_non_empty_image(tmp_path: Path) -> None:
    image_path = tmp_path / "merged-binary.bin"
    image_path.write_bytes(b"xiaoyao-firmware-image")

    completed = run_check(image_path)

    assert completed.returncode == 0, completed.stderr
    result = parse_json_output(completed)
    assert result == {
        "image_path": str(image_path.resolve()),
        "length_bytes": image_path.stat().st_size,
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest().upper(),
        "expected_sha256": None,
        "sha256_matches_expected": True,
    }


def test_artifact_check_rejects_missing_empty_and_mismatched_images(tmp_path: Path) -> None:
    missing = run_check(tmp_path / "missing.bin")
    assert missing.returncode != 0
    assert "does not exist" in missing.stderr

    empty_path = tmp_path / "empty.bin"
    empty_path.touch()
    empty = run_check(empty_path)
    assert empty.returncode != 0
    assert "is empty" in empty.stderr

    image_path = tmp_path / "merged-binary.bin"
    image_path.write_bytes(b"xiaoyao-firmware-image")
    mismatch = run_check(image_path, "0" * 64)
    assert mismatch.returncode != 0
    assert "does not match" in mismatch.stderr


def test_artifact_check_rejects_non_sha256_expected_hash(tmp_path: Path) -> None:
    image_path = tmp_path / "merged-binary.bin"
    image_path.write_bytes(b"xiaoyao-firmware-image")

    completed = run_check(image_path, "not-a-sha256")

    assert completed.returncode != 0
    assert "64 hexadecimal" in completed.stderr


def test_artifact_check_rejects_an_explicit_blank_expected_hash(tmp_path: Path) -> None:
    image_path = tmp_path / "merged-binary.bin"
    image_path.write_bytes(b"xiaoyao-firmware-image")

    completed = run_check(image_path, "")

    assert completed.returncode != 0
    assert "64 hexadecimal" in completed.stderr
