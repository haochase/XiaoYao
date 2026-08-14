import json
from pathlib import Path

import pytest

from tools.firmware_profile import (
    ProfileError,
    render_build_config,
    select_vendor_root,
    validate_ota_url,
)


def test_select_vendor_root_prefers_a_vendor_directory_in_the_workspace(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "main-checkout"
    local_vendor = workspace_root / ".vendor"
    (local_vendor / "xiaozhi-esp32-main").mkdir(parents=True)
    (tmp_path / ".vendor" / "xiaozhi-esp32-main").mkdir(parents=True)

    assert select_vendor_root(workspace_root) == local_vendor


def test_select_vendor_root_falls_back_from_a_worktree_to_the_repository_vendor(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / ".worktrees" / "ota-bootstrap"
    repository_vendor = tmp_path / ".vendor"
    (repository_vendor / "xiaozhi-esp32-main").mkdir(parents=True)

    assert select_vendor_root(workspace_root) == repository_vendor


def test_xiaoyao_patch_has_no_trailing_whitespace() -> None:
    patch_path = (
        Path(__file__).resolve().parents[2]
        / "firmware"
        / "patches"
        / "0001-xiaoyao-waveshare-profile.patch"
    )

    assert all(
        not line.endswith((" \n", "\t\n"))
        for line in patch_path.read_text(encoding="utf-8").splitlines(keepends=True)
    )


def test_validate_ota_url_accepts_http_and_https_urls() -> None:
    assert validate_ota_url("https://example.com/ota") == "https://example.com/ota"
    assert validate_ota_url("http://example.com:8080/ota") == "http://example.com:8080/ota"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/ota",
        "https:///ota",
        "https://user@example.com/ota",
        "https://user:password@example.com/ota",
        "https://example.com/ota?channel=dev",
        "https://example.com/ota#fragment",
        'https://example.com/"CONFIG_X=y',
        r"https://example.com/\path",
    ],
)
def test_validate_ota_url_rejects_unsafe_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(ProfileError):
        validate_ota_url(url)


def test_render_build_config_adds_only_the_validated_ota_setting(tmp_path: Path) -> None:
    template_path = tmp_path / "xiaoyao.config.json"
    template = {
        "manufacturer": "waveshare",
        "type": "esp32-s3-audio-board",
        "target": "esp32s3",
        "builds": [
            {
                "name": "esp32-s3-audio-board",
                "sdkconfig_append": [
                    "CONFIG_USE_CUSTOM_WAKE_WORD=y",
                    'CONFIG_CUSTOM_WAKE_WORD="ni hao xiao yao"',
                ],
            }
        ],
    }
    template_path.write_text(json.dumps(template), encoding="utf-8")

    rendered = json.loads(
        render_build_config(template_path, "https://example.com/ota")
    )

    assert rendered["builds"][0]["sdkconfig_append"] == [
        "CONFIG_USE_CUSTOM_WAKE_WORD=y",
        'CONFIG_CUSTOM_WAKE_WORD="ni hao xiao yao"',
        'CONFIG_OTA_URL="https://example.com/ota"',
    ]
    assert json.loads(template_path.read_text(encoding="utf-8")) == template
