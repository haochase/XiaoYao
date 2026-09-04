import json
from pathlib import Path
import re

import pytest
import tools.firmware_profile as firmware_profile

from tools.firmware_profile import (
    ProfileError,
    render_build_config,
    select_vendor_root,
    validate_ota_url,
)


def test_firmware_profile_defers_annotations_for_python_39() -> None:
    source_path = Path(firmware_profile.__file__)
    future_import = source_path.read_text(encoding="utf-8").splitlines()[1]

    assert future_import == "from __future__ import annotations"


def test_apply_vendor_profile_updates_known_upstream_boundaries(tmp_path: Path) -> None:
    source_root = tmp_path / "xiaozhi"
    main = source_root / "main"
    main.mkdir(parents=True)
    scripts = source_root / "scripts"
    scripts.mkdir()
    protocols = main / "protocols"
    protocols.mkdir()
    kconfig_path = main / "Kconfig.projbuild"
    application_path = main / "application.cc"
    application_header_path = main / "application.h"
    protocol_header_path = protocols / "protocol.h"
    protocol_source_path = protocols / "protocol.cc"
    websocket_source_path = protocols / "websocket_protocol.cc"
    build_path = scripts / "build.py"
    kconfig_path.write_text(
        "menu \"Xiaozhi Assistant\"\n\n"
        "config OTA_URL\n"
        "    string \"Default OTA URL\"\n"
        "    default \"https://api.tenclass.net/xiaozhi/ota/\"\n"
        "    help\n"
        "        The application will access this URL to check for new firmwares and server address.\n\n"
        "choice\n",
        encoding="utf-8",
    )
    application_path.write_text(
        "void Application::Initialize() {\n"
        "    AudioServiceCallbacks callbacks;\n"
        "    callbacks.on_vad_change = [this](bool speaking) {\n"
        "        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);\n"
        "    };\n"
        "    audio_service_.SetCallbacks(callbacks);\n"
        "}\n"
        "\n"
        "void Application::InitializeProtocol() {\n"
        "    if (ota_->HasMqttConfig()) {\n"
        "        protocol_ = std::make_unique<MqttProtocol>();\n"
        "    } else if (ota_->HasWebsocketConfig()) {\n"
        "        protocol_ = std::make_unique<WebsocketProtocol>();\n"
        "    } else {\n"
        "        ESP_LOGW(TAG, \"No protocol specified in the OTA config, using MQTT\");\n"
        "        protocol_ = std::make_unique<MqttProtocol>();\n"
        "    }\n"
        "}\n"
        "\n"
        "void Application::HandleStateChangedEvent() {\n"
        "    switch (GetDeviceState()) {\n"
        "        case kDeviceStateSpeaking:\n"
        "            if (listening_mode_ != kListeningModeRealtime) {\n"
        "                audio_service_.EnableVoiceProcessing(false);\n"
        "                // Only AFE wake word can be detected in speaking mode\n"
        "                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());\n"
        "            }\n"
        "            break;\n"
            "    }\n"
            "}\n"
            "\n"
            "void Application::RunClockFixture() {\n"
            + firmware_profile._CLOCK_TICK_ANCHOR
            + "    }\n"
            "}\n"
            "\n"
            + firmware_profile._NETWORK_CONNECTED_ANCHOR
            + "}\n"
            "\n"
            + firmware_profile._NETWORK_DISCONNECTED_ANCHOR
            + "}\n"
            "\n"
            "void Application::HandleActivationDoneEvent() {\n"
            + firmware_profile._ACTIVATION_DONE_ANCHOR
            + "}\n"
            "\n"
            "void Application::HandleTtsFixture() {\n"
            + firmware_profile._TTS_STATE_ANCHOR
            + "}\n"
            "\n"
            + firmware_profile._CONTINUE_CHANNEL_ANCHOR
            + "}\n",
            encoding="utf-8",
        )
    application_header_path.write_text(
        "class Application {\n"
        + firmware_profile._APP_FIELDS_ANCHOR
        + firmware_profile._APP_METHODS_ANCHOR
        + "};\n",
        encoding="utf-8",
    )
    protocol_header_path.write_text(
        "class Protocol {\n"
        "public:\n"
        "    virtual void SendStartListening(ListeningMode mode);\n"
        "    virtual void SendStopListening();\n"
        "    virtual void SendAbortSpeaking(AbortReason reason);\n"
        "};\n",
        encoding="utf-8",
    )
    protocol_source_path.write_text(
        "void Protocol::SendStopListening() {\n"
        "    std::string message =\n"
        "        \"{\\\"session_id\\\":\\\"\" + session_id_ + \"\\\",\\\"type\\\":\\\"listen\\\",\\\"state\\\":\\\"stop\\\"}\";\n"
        "    SendText(message);\n"
        "}\n"
        "\n"
        "void Protocol::SendMcpMessage(const std::string& payload) {\n",
        encoding="utf-8",
    )
    websocket_source_path.write_text(
        "std::string WebsocketProtocol::GetHelloMessage() {\n"
        "    cJSON* features = cJSON_CreateObject();\n"
        "    cJSON_AddBoolToObject(features, \"mcp\", true);\n"
        "    cJSON_AddItemToObject(root, \"features\", features);\n"
        "}\n",
        encoding="utf-8",
    )
    build_path.write_text(
        "def _run_idf():\n"
        "    command = [\"idf.py\"]\n",
        encoding="utf-8",
    )

    firmware_profile.apply_vendor_profile(source_root)
    firmware_profile.apply_vendor_profile(source_root)

    kconfig = kconfig_path.read_text(encoding="utf-8")
    assert "config XIAOYAO_WEBSOCKET_ONLY" in kconfig
    assert "config XIAOYAO_VAD_EVENTS" in kconfig
    application = application_path.read_text(encoding="utf-8")
    assert "#if CONFIG_XIAOYAO_WEBSOCKET_ONLY" in application
    assert "#endif" in application
    assert "#if CONFIG_USE_CUSTOM_WAKE_WORD" in application
    assert "audio_service_.EnableWakeWordDetection(false);" in application
    assert "protocol_->SendVadState(speaking);" in application
    assert "EnsureIdleControlChannel();" in application
    assert "clock_ticks_ % 5 == 0" in application
    assert "notification_tts_ = notification;" in application
    assert "protocol_->SendTtsReady();" in application
    application_header = application_header_path.read_text(encoding="utf-8")
    assert "bool network_connected_ = false;" in application_header
    assert "void EnsureIdleControlChannel();" in application_header
    protocol_header = protocol_header_path.read_text(encoding="utf-8")
    assert "virtual void SendVadState(bool speaking);" in protocol_header
    protocol_source = protocol_source_path.read_text(encoding="utf-8")
    assert r'\"type\":\"vad\"' in protocol_source
    assert 'speaking ? "start" : "stop"' in protocol_source
    websocket_source = websocket_source_path.read_text(encoding="utf-8")
    assert 'cJSON_AddBoolToObject(features, "vad_events", true);' in websocket_source
    assert 'os.environ.get("XIAOYAO_IDF_COMMAND", "idf.py")' in build_path.read_text(
        encoding="utf-8"
    )


def test_public_xiaoyao_profile_selects_an_esp32s3_chinese_multinet_model() -> None:
    template_path = Path(__file__).resolve().parents[2] / "firmware" / "xiaoyao.config.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))

    assert "CONFIG_SR_MN_CN_MULTINET6_QUANT=y" in template["builds"][0][
        "sdkconfig_append"
    ]
    assert "CONFIG_XIAOYAO_VAD_EVENTS=y" in template["builds"][0][
        "sdkconfig_append"
    ]
    assert "CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL=y" in template["builds"][0][
        "sdkconfig_append"
    ]
    assert "CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=50" in template["builds"][0][
        "sdkconfig_append"
    ]


def test_vendor_profile_contains_persistent_control_channel_boundaries() -> None:
    source = Path(firmware_profile.__file__).read_text(encoding="utf-8")

    assert "config XIAOYAO_PERSISTENT_CONTROL_CHANNEL" in source
    assert "EnsureIdleControlChannel" in source
    assert "clock_ticks_ % 5 == 0" in firmware_profile._CLOCK_TICK_PROFILE
    assert (
        'strcmp(purpose->valuestring, "notification") == 0'
        in firmware_profile._TTS_STATE_PROFILE
    )


def test_apply_vendor_profile_accepts_a_semantically_complete_evolved_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "evolved-xiaozhi"
    for relative_path, markers in firmware_profile._VENDOR_PROFILE_MARKERS.items():
        path = source_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "// source may contain additional compatible behavior\n"
            + "\n".join(markers)
            + "\n",
            encoding="utf-8",
        )

    firmware_profile.apply_vendor_profile(source_root)


def test_exact_profile_can_upgrade_a_previous_rendered_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "Kconfig.projbuild"
    previous_profile = "config XIAOYAO_WEBSOCKET_ONLY\n\nchoice\n"
    current_profile = (
        "config XIAOYAO_WEBSOCKET_ONLY\n\n"
        "config XIAOYAO_VAD_EVENTS\n\n"
        "choice\n"
    )
    profile_path.write_text(previous_profile, encoding="utf-8")

    firmware_profile._apply_exact_profile(
        profile_path,
        "choice\n",
        current_profile,
        previous_profiles=(previous_profile,),
    )

    assert profile_path.read_text(encoding="utf-8") == current_profile


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


def test_firmware_build_script_requires_a_single_interpreter_and_profile_output() -> None:
    build_script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "build-xiaozhi-waveshare.ps1"
    ).read_text(encoding="utf-8")

    compact_script = re.sub(r"\s+", "", build_script)

    assert "functionResolve-ProfilePython" in compact_script
    assert "$hostPython=Resolve-ProfilePython" in compact_script
    assert "--select-vendor-root" in build_script
    assert "Unable to render the temporary XiaoYao profile" in build_script
    assert "$buildSdkconfig = Join-Path $xiaozhiRoot 'sdkconfig'" in build_script
    assert 'CONFIG_USE_CUSTOM_WAKE_WORD=y' in build_script
    assert 'CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=50' in build_script
    assert 'CONFIG_XIAOYAO_WEBSOCKET_ONLY=y' in build_script
    assert 'CONFIG_XIAOYAO_VAD_EVENTS=y' in build_script
    assert 'CONFIG_SR_MN_CN_MULTINET6_QUANT=y' in build_script
    assert "Copy-Item -Force $idfExecutable $shimPath" not in build_script
    assert "& $idfExecutable fullclean" in build_script
    assert 'XIAOYAO_IDF_COMMAND = $idfExecutable' in build_script
