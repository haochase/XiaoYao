"""Render a local XiaoYao firmware profile without persisting its OTA endpoint."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


class ProfileError(ValueError):
    """Raised when a firmware profile cannot be rendered safely."""


_KCONFIG_ANCHOR = (
    "        The application will access this URL to check for new firmwares and server address.\n\n"
    "choice\n"
)
_KCONFIG_PROFILE = (
    "        The application will access this URL to check for new firmwares and server address.\n\n"
    "config XIAOYAO_WEBSOCKET_ONLY\n"
    "    bool \"Force WebSocket protocol\"\n"
    "    default n\n"
    "    help\n"
    "        Use WebSocket after activation even when OTA does not provide protocol\n"
    "        configuration. This prevents a failed OTA round from falling back to MQTT.\n\n"
    "choice\n"
)
_PROTOCOL_ANCHOR = (
    "    if (ota_->HasMqttConfig()) {\n"
    "        protocol_ = std::make_unique<MqttProtocol>();\n"
    "    } else if (ota_->HasWebsocketConfig()) {\n"
    "        protocol_ = std::make_unique<WebsocketProtocol>();\n"
    "    } else {\n"
    "        ESP_LOGW(TAG, \"No protocol specified in the OTA config, using MQTT\");\n"
    "        protocol_ = std::make_unique<MqttProtocol>();\n"
    "    }\n"
)
_PROTOCOL_PROFILE = (
    "    #if CONFIG_XIAOYAO_WEBSOCKET_ONLY\n"
    "    protocol_ = std::make_unique<WebsocketProtocol>();\n"
    "    #else\n"
    + _PROTOCOL_ANCHOR
    + "    #endif\n"
)
_BUILD_IDF_ANCHOR = '    command = ["idf.py"]\n'
_BUILD_IDF_PROFILE = (
    '    command = [os.environ.get("XIAOYAO_IDF_COMMAND", "idf.py")]\n'
)


def _read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as file:
        return file.read()


def _write_text_preserving_newlines(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(content)


def _apply_exact_profile(path: Path, anchor: str, replacement: str) -> None:
    try:
        content = _read_text_preserving_newlines(path)
    except OSError as exc:
        raise ProfileError(f"unable to read vendor source: {path}") from exc

    line_ending = "\r\n" if "\r\n" in content else "\n"
    expected = anchor.replace("\n", line_ending)
    rendered = replacement.replace("\n", line_ending)
    if rendered in content:
        return
    if expected not in content:
        raise ProfileError(f"vendor source does not match the expected profile anchor: {path}")
    _write_text_preserving_newlines(path, content.replace(expected, rendered, 1))


def apply_vendor_profile(source_root: Path) -> None:
    """Apply the XiaoYao protocol profile to a known XiaoZhi source snapshot."""
    source_root = source_root.resolve()
    _apply_exact_profile(
        source_root / "main" / "Kconfig.projbuild",
        _KCONFIG_ANCHOR,
        _KCONFIG_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _PROTOCOL_ANCHOR,
        _PROTOCOL_PROFILE,
    )
    _apply_exact_profile(
        source_root / "scripts" / "build.py",
        _BUILD_IDF_ANCHOR,
        _BUILD_IDF_PROFILE,
    )


def select_vendor_root(workspace_root: Path) -> Path:
    """Find the vendor directory for a checkout or a sibling worktree."""
    workspace_root = workspace_root.resolve()
    local_vendor = workspace_root / ".vendor"
    if (local_vendor / "xiaozhi-esp32-main").is_dir():
        return local_vendor

    repository_vendor = workspace_root.parent.parent / ".vendor"
    if (repository_vendor / "xiaozhi-esp32-main").is_dir():
        return repository_vendor

    raise ProfileError(
        "xiaozhi source snapshot was not found in the workspace or repository vendor directory"
    )


def validate_ota_url(ota_url: str) -> str:
    """Return a supported OTA endpoint or raise ``ProfileError``."""
    if not isinstance(ota_url, str) or not ota_url:
        raise ProfileError("OTA URL must be a non-empty string")
    if any(character.isspace() for character in ota_url):
        raise ProfileError("OTA URL must not contain whitespace")
    if '"' in ota_url or "\\" in ota_url:
        raise ProfileError("OTA URL must not contain Kconfig escape characters")

    parsed = urlsplit(ota_url)
    if parsed.scheme not in {"http", "https"}:
        raise ProfileError("OTA URL must use http or https")
    if not parsed.hostname:
        raise ProfileError("OTA URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ProfileError("OTA URL must not include user credentials")
    if parsed.query or parsed.fragment:
        raise ProfileError("OTA URL must not include a query or fragment")

    try:
        parsed.port
    except ValueError as exc:
        raise ProfileError("OTA URL contains an invalid port") from exc
    return ota_url


def render_build_config(template_path: Path, ota_url: str) -> str:
    """Render a build config that differs from its template only by ``OTA_URL``."""
    validated_url = validate_ota_url(ota_url)
    try:
        config = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"unable to read profile template: {template_path}") from exc

    builds = config.get("builds") if isinstance(config, dict) else None
    if not isinstance(builds, list) or not builds:
        raise ProfileError("profile template must contain at least one build")

    ota_setting = f'CONFIG_OTA_URL="{validated_url}"'
    for build in builds:
        if not isinstance(build, dict):
            raise ProfileError("profile template builds must be objects")
        sdkconfig_append = build.get("sdkconfig_append")
        if not isinstance(sdkconfig_append, list) or not all(
            isinstance(option, str) for option in sdkconfig_append
        ):
            raise ProfileError("profile template sdkconfig_append must be a string list")
        if any(option.startswith("CONFIG_OTA_URL=") for option in sdkconfig_append):
            raise ProfileError("profile template must not contain CONFIG_OTA_URL")
        sdkconfig_append.append(ota_setting)

    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a temporary XiaoYao firmware configuration."
    )
    parser.add_argument("--template", type=Path)
    parser.add_argument("--ota-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--select-vendor-root", type=Path)
    parser.add_argument("--apply-vendor-profile", type=Path)
    args = parser.parse_args(argv)

    if args.select_vendor_root is not None:
        if any((args.template, args.ota_url, args.output)):
            parser.error("--select-vendor-root cannot be combined with render options")
        try:
            print(select_vendor_root(args.select_vendor_root))
        except ProfileError as exc:
            parser.error(str(exc))
        return 0

    if args.apply_vendor_profile is not None:
        if any((args.template, args.ota_url, args.output)):
            parser.error("--apply-vendor-profile cannot be combined with render options")
        try:
            apply_vendor_profile(args.apply_vendor_profile)
        except ProfileError as exc:
            parser.error(str(exc))
        return 0

    if args.template is None or args.ota_url is None or args.output is None:
        parser.error("--template, --ota-url, and --output are required for rendering")

    try:
        rendered = render_build_config(args.template, args.ota_url)
        args.output.write_text(rendered, encoding="utf-8")
    except (OSError, ProfileError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
