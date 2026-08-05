import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class ProbeError(RuntimeError):
    """Raised when a read-only board probe cannot produce reliable facts."""


_MAC_PATTERN = re.compile(
    r"(?im)^(?P<prefix>\s*MAC:\s*)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\s*$"
)


def redact_sensitive_output(output: str) -> str:
    return _MAC_PATTERN.sub(r"\g<prefix>[redacted]", output)


def parse_arduino_board_list(output: str, requested_port: str) -> dict[str, str]:
    try:
        document = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ProbeError("arduino-cli returned invalid JSON") from exc

    for detected in document.get("detected_ports", []):
        port = detected.get("port", {})
        address = str(port.get("address", ""))
        if address.casefold() != requested_port.casefold():
            continue

        boards = detected.get("matching_boards", [])
        board = boards[0] if boards else {}
        properties = port.get("properties", {})
        return {
            "port": address,
            "board_name": str(board.get("name", "Unknown")),
            "fqbn": str(board.get("fqbn", "")),
            "protocol": str(port.get("protocol", "")),
            "protocol_label": str(port.get("protocol_label", "")),
            "vid": str(properties.get("vid", "")),
            "pid": str(properties.get("pid", "")),
        }

    raise ProbeError(f"port {requested_port} was not found")


def _required_match(pattern: str, output: str, field: str) -> re.Match[str]:
    match = re.search(pattern, output, flags=re.IGNORECASE)
    if match is None:
        raise ProbeError(f"esptool output did not contain {field}")
    return match


def parse_esptool_chip_id(output: str) -> dict[str, str]:
    chip = _required_match(
        r"Chip type:\s*([A-Za-z0-9-]+)\s+\(([^)]+)\)\s+\(revision\s+([^)]+)\)",
        output,
        "chip type",
    )
    psram = _required_match(
        r"Embedded PSRAM\s+([0-9]+MB)",
        output,
        "PSRAM size",
    )
    crystal = _required_match(
        r"Crystal frequency:\s*([^\r\n]+)",
        output,
        "crystal frequency",
    )
    usb_mode = _required_match(
        r"USB mode:\s*([^\r\n]+)",
        output,
        "USB mode",
    )
    return {
        "chip": chip.group(1).strip(),
        "package": chip.group(2).strip(),
        "revision": chip.group(3).strip(),
        "psram": psram.group(1).strip(),
        "crystal": crystal.group(1).strip(),
        "usb_mode": usb_mode.group(1).strip(),
    }


def parse_esptool_flash_id(output: str) -> dict[str, str]:
    flash = _required_match(
        r"Detected flash size:\s*([^\r\n]+)",
        output,
        "flash size",
    )
    flash_type = _required_match(
        r"Flash type set in eFuse:\s*([^\r\n]+)",
        output,
        "flash type",
    )
    flash_voltage = _required_match(
        r"Flash voltage set by eFuse:\s*([^\r\n]+)",
        output,
        "flash voltage",
    )
    return {
        "flash": flash.group(1).strip(),
        "flash_type": flash_type.group(1).strip(),
        "flash_voltage": flash_voltage.group(1).strip(),
    }


def build_esptool_commands(port: str, python_executable: str) -> list[list[str]]:
    base = [python_executable, "-m", "esptool", "--port", port]
    return [[*base, "chip-id"], [*base, "flash-id"]]


def run_command(command: list[str], *, timeout_seconds: int = 20) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            f"command timed out after {timeout_seconds} seconds"
        ) from exc
    except OSError as exc:
        raise ProbeError(f"could not start command: {command[0]}") from exc

    if completed.returncode != 0:
        detail = redact_sensitive_output(
            "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        ).strip()
        if len(detail) > 500:
            detail = f"{detail[:500]}..."
        raise ProbeError(
            f"command failed with exit code {completed.returncode}: {detail}"
        )
    return completed.stdout


def discover_arduino_cli() -> str:
    discovered = shutil.which("arduino-cli")
    if discovered:
        return discovered

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        portable = (
            Path(user_profile)
            / ".codex"
            / "tools"
            / "arduino-cli"
            / "arduino-cli.exe"
        )
        if portable.is_file():
            return str(portable)

    raise ProbeError("arduino-cli was not found")


def probe(
    port: str,
    *,
    arduino_cli: str,
    python_executable: str,
) -> dict[str, object]:
    board_output = run_command(
        [arduino_cli, "board", "list", "--format", "json"]
    )
    board = parse_arduino_board_list(board_output, port)
    chip_command, flash_command = build_esptool_commands(port, python_executable)
    chip = parse_esptool_chip_id(run_command(chip_command))
    flash = parse_esptool_flash_id(run_command(flash_command))
    return {
        "status": "ok",
        "board": board,
        "chip": chip,
        "flash": flash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read ESP32 identity and capacity without writing flash."
    )
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--arduino-cli")
    parser.add_argument("--python", default=sys.executable, dest="python_executable")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = probe(
            args.port,
            arduino_cli=args.arduino_cli or discover_arduino_cli(),
            python_executable=args.python_executable,
        )
    except ProbeError as exc:
        result = {"status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False) if args.json else result["error"])
        return 1

    print(
        json.dumps(result, ensure_ascii=False, indent=2)
        if args.json
        else result
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
