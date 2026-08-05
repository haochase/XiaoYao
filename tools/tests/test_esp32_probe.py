import json
import subprocess
import sys

import pytest

from tools.esp32_probe import (
    ProbeError,
    build_esptool_commands,
    parse_arduino_board_list,
    parse_esptool_chip_id,
    parse_esptool_flash_id,
    redact_sensitive_output,
    run_command,
)


ARDUINO_OUTPUT = json.dumps(
    {
        "detected_ports": [
            {
                "matching_boards": [
                    {
                        "name": "ESP32 Family Device",
                        "fqbn": "esp32:esp32:esp32_family",
                        "is_hidden": True,
                    }
                ],
                "port": {
                    "address": "COM7",
                    "label": "COM7",
                    "protocol": "serial",
                    "protocol_label": "Serial Port (USB)",
                    "properties": {
                        "pid": "0x1001",
                        "vid": "0x303A",
                    },
                },
            }
        ]
    }
)

CHIP_OUTPUT = """
Connected to ESP32-S3 on COM7:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, \
Embedded PSRAM 8MB (AP_3v3)
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG
MAC:                aa:bb:cc:dd:ee:ff
"""

FLASH_OUTPUT = """
Flash Memory Information:
Manufacturer: 20
Device: 4018
Detected flash size: 16MB
Flash type set in eFuse: quad (4 data lines)
Flash voltage set by eFuse: 3.3V
"""


def test_parse_arduino_board_list_selects_requested_port() -> None:
    facts = parse_arduino_board_list(ARDUINO_OUTPUT, "com7")

    assert facts == {
        "port": "COM7",
        "board_name": "ESP32 Family Device",
        "fqbn": "esp32:esp32:esp32_family",
        "protocol": "serial",
        "protocol_label": "Serial Port (USB)",
        "vid": "0x303A",
        "pid": "0x1001",
    }


def test_parse_arduino_board_list_rejects_absent_port() -> None:
    with pytest.raises(ProbeError, match="port COM8 was not found"):
        parse_arduino_board_list(ARDUINO_OUTPUT, "COM8")


def test_parse_chip_id_returns_sanitized_hardware_facts() -> None:
    facts = parse_esptool_chip_id(CHIP_OUTPUT)

    assert facts == {
        "chip": "ESP32-S3",
        "package": "QFN56",
        "revision": "v0.2",
        "psram": "8MB",
        "crystal": "40MHz",
        "usb_mode": "USB-Serial/JTAG",
    }
    assert "MAC" not in json.dumps(facts)


def test_parse_flash_id_returns_capacity_and_bus_details() -> None:
    assert parse_esptool_flash_id(FLASH_OUTPUT) == {
        "flash": "16MB",
        "flash_type": "quad (4 data lines)",
        "flash_voltage": "3.3V",
    }


def test_redaction_removes_mac_addresses() -> None:
    redacted = redact_sensitive_output(CHIP_OUTPUT)

    assert "aa:bb:cc:dd:ee:ff" not in redacted
    assert "MAC:                [redacted]" in redacted


def test_esptool_commands_are_fixed_to_read_only_operations() -> None:
    commands = build_esptool_commands("COM7", sys.executable)

    assert commands == [
        [sys.executable, "-m", "esptool", "--port", "COM7", "chip-id"],
        [sys.executable, "-m", "esptool", "--port", "COM7", "flash-id"],
    ]
    flattened = " ".join(part for command in commands for part in command)
    assert "write" not in flattened
    assert "erase" not in flattened


def test_command_timeout_becomes_structured_probe_error(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ProbeError, match="command timed out after 3 seconds"):
        run_command(["arduino-cli", "board", "list"], timeout_seconds=3)
