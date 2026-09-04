"""Render a local XiaoYao firmware profile without persisting its OTA endpoint."""
from __future__ import annotations

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
_KCONFIG_WEBSOCKET_ONLY_PROFILE = (
    "        The application will access this URL to check for new firmwares and server address.\n\n"
    "config XIAOYAO_WEBSOCKET_ONLY\n"
    "    bool \"Force WebSocket protocol\"\n"
    "    default n\n"
    "    help\n"
    "        Use WebSocket after activation even when OTA does not provide protocol\n"
    "        configuration. This prevents a failed OTA round from falling back to MQTT.\n\n"
    "choice\n"
)
_KCONFIG_VAD_PROFILE = (
    "        The application will access this URL to check for new firmwares and server address.\n\n"
    "config XIAOYAO_WEBSOCKET_ONLY\n"
    "    bool \"Force WebSocket protocol\"\n"
    "    default n\n"
    "    help\n"
    "        Use WebSocket after activation even when OTA does not provide protocol\n"
    "        configuration. This prevents a failed OTA round from falling back to MQTT.\n\n"
    "config XIAOYAO_VAD_EVENTS\n"
    "    bool \"Send AFE VAD events to the XiaoYao gateway\"\n"
    "    default n\n"
    "    depends on USE_AUDIO_PROCESSOR\n"
    "    help\n"
    "        Advertise VAD event support and send speech start and stop controls.\n\n"
    "choice\n"
)
_KCONFIG_PROFILE = _KCONFIG_VAD_PROFILE.replace(
    "choice\n",
    "config XIAOYAO_PERSISTENT_CONTROL_CHANNEL\n"
    "    bool \"Keep a low-traffic control channel while idle\"\n"
    "    default n\n"
    "    depends on XIAOYAO_WEBSOCKET_ONLY\n"
    "    help\n"
    "        Connect after activation and reconnect while idle so reminders can\n"
    "        play without a wake word. Microphone audio remains disabled in idle.\n\n"
    "choice\n",
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
_SPEAKING_WAKE_WORD_ANCHOR = (
    "            if (listening_mode_ != kListeningModeRealtime) {\n"
    "                audio_service_.EnableVoiceProcessing(false);\n"
    "                // Only AFE wake word can be detected in speaking mode\n"
    "                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());\n"
    "            }\n"
)
_SPEAKING_WAKE_WORD_PROFILE = (
    "            if (listening_mode_ != kListeningModeRealtime) {\n"
    "                audio_service_.EnableVoiceProcessing(false);\n"
    "                // Custom wake words cannot safely distinguish speaker feedback.\n"
    "                #if CONFIG_USE_CUSTOM_WAKE_WORD\n"
    "                audio_service_.EnableWakeWordDetection(false);\n"
    "                #else\n"
    "                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());\n"
    "                #endif\n"
    "            }\n"
)
_VAD_CALLBACK_ANCHOR = (
    "    callbacks.on_vad_change = [this](bool speaking) {\n"
    "        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);\n"
    "    };\n"
)
_VAD_CALLBACK_PROFILE = (
    "    callbacks.on_vad_change = [this](bool speaking) {\n"
    "#if CONFIG_XIAOYAO_VAD_EVENTS\n"
    "        Schedule([this, speaking]() {\n"
    "            auto state = GetDeviceState();\n"
    "            bool accepts_vad = state == kDeviceStateListening ||\n"
    "                (state == kDeviceStateSpeaking &&\n"
    "                 listening_mode_ == kListeningModeRealtime);\n"
    "            if (accepts_vad && protocol_ && protocol_->IsAudioChannelOpened()) {\n"
    "                protocol_->SendVadState(speaking);\n"
    "            }\n"
    "        });\n"
    "#endif\n"
    "        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);\n"
    "    };\n"
)
_PLAYBACK_DRAINED_ANCHOR = (
    "    callbacks.on_playback_drained = [this]() {\n"
    "        xEventGroupSetBits(event_group_, MAIN_EVENT_PLAYBACK_DRAINED);\n"
    "    };\n"
)
_PLAYBACK_DRAINED_PROFILE = (
    "    callbacks.on_playback_drained = [this]() {\n"
    "#if CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL\n"
    "        Schedule([this]() { FinishNotificationIfPlaybackDrained(); });\n"
    "#endif\n"
    "        xEventGroupSetBits(event_group_, MAIN_EVENT_PLAYBACK_DRAINED);\n"
    "    };\n"
)
_PROTOCOL_HEADER_ANCHOR = (
    "    virtual void SendStartListening(ListeningMode mode);\n"
    "    virtual void SendStopListening();\n"
    "    virtual void SendAbortSpeaking(AbortReason reason);\n"
)
_PROTOCOL_HEADER_VAD_PROFILE = (
    "    virtual void SendStartListening(ListeningMode mode);\n"
    "    virtual void SendStopListening();\n"
    "    virtual void SendVadState(bool speaking);\n"
    "    virtual void SendAbortSpeaking(AbortReason reason);\n"
)
_PROTOCOL_HEADER_READY_PROFILE = _PROTOCOL_HEADER_VAD_PROFILE.replace(
    "    virtual void SendAbortSpeaking(AbortReason reason);\n",
    "    virtual void SendTtsReady();\n"
    "    virtual void SendAbortSpeaking(AbortReason reason);\n"
)
_PROTOCOL_HEADER_PROFILE = _PROTOCOL_HEADER_READY_PROFILE.replace(
    "    virtual void SendAbortSpeaking(AbortReason reason);\n",
    "    virtual void SendTtsDone();\n"
    "    virtual void SendAbortSpeaking(AbortReason reason);\n",
)
_PROTOCOL_SOURCE_ANCHOR = (
    "void Protocol::SendStopListening() {\n"
    "    std::string message =\n"
    "        \"{\\\"session_id\\\":\\\"\" + session_id_ + \"\\\",\\\"type\\\":\\\"listen\\\",\\\"state\\\":\\\"stop\\\"}\";\n"
    "    SendText(message);\n"
    "}\n\n"
    "void Protocol::SendMcpMessage(const std::string& payload) {\n"
)
_PROTOCOL_SOURCE_VAD_PROFILE = (
    "void Protocol::SendStopListening() {\n"
    "    std::string message =\n"
    "        \"{\\\"session_id\\\":\\\"\" + session_id_ + \"\\\",\\\"type\\\":\\\"listen\\\",\\\"state\\\":\\\"stop\\\"}\";\n"
    "    SendText(message);\n"
    "}\n\n"
    "void Protocol::SendVadState(bool speaking) {\n"
    "    std::string message = \"{\\\"session_id\\\":\\\"\" + session_id_ +\n"
    "                          \"\\\",\\\"type\\\":\\\"vad\\\",\\\"state\\\":\\\"\";\n"
    "    message += speaking ? \"start\" : \"stop\";\n"
    "    message += \"\\\"}\";\n"
    "    SendText(message);\n"
    "}\n\n"
    "void Protocol::SendMcpMessage(const std::string& payload) {\n"
)
_PROTOCOL_SOURCE_READY_PROFILE = _PROTOCOL_SOURCE_VAD_PROFILE.replace(
    "void Protocol::SendMcpMessage(const std::string& payload) {\n",
    "void Protocol::SendTtsReady() {\n"
    "    std::string message = \"{\\\"session_id\\\":\\\"\" + session_id_ +\n"
    "                          \"\\\",\\\"type\\\":\\\"tts\\\",\\\"state\\\":\\\"ready\\\"}\";\n"
    "    SendText(message);\n"
    "}\n\n"
    "void Protocol::SendMcpMessage(const std::string& payload) {\n",
)
_PROTOCOL_SOURCE_PROFILE = _PROTOCOL_SOURCE_READY_PROFILE.replace(
    "void Protocol::SendMcpMessage(const std::string& payload) {\n",
    "void Protocol::SendTtsDone() {\n"
    "    std::string message = \"{\\\"session_id\\\":\\\"\" + session_id_ +\n"
    "                          \"\\\",\\\"type\\\":\\\"tts\\\",\\\"state\\\":\\\"done\\\"}\";\n"
    "    SendText(message);\n"
    "}\n\n"
    "void Protocol::SendMcpMessage(const std::string& payload) {\n",
)
_WEBSOCKET_FEATURE_ANCHOR = (
    "    cJSON_AddBoolToObject(features, \"mcp\", true);\n"
    "    cJSON_AddItemToObject(root, \"features\", features);\n"
)
_WEBSOCKET_FEATURE_PROFILE = (
    "    cJSON_AddBoolToObject(features, \"mcp\", true);\n"
    "#if CONFIG_XIAOYAO_VAD_EVENTS\n"
    "    cJSON_AddBoolToObject(features, \"vad_events\", true);\n"
    "#endif\n"
    "    cJSON_AddItemToObject(root, \"features\", features);\n"
)
_CLOCK_TICK_ANCHOR = (
    "        if (bits & MAIN_EVENT_CLOCK_TICK) {\n"
    "            clock_ticks_++;\n"
    "            auto display = Board::GetInstance().GetDisplay();\n"
    "            display->UpdateStatusBar();\n\n"
)
_CLOCK_TICK_PROFILE = _CLOCK_TICK_ANCHOR + (
    "#if CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL\n"
    "            if (clock_ticks_ % 5 == 0) {\n"
    "                EnsureIdleControlChannel();\n"
    "            }\n"
    "#endif\n\n"
)
_NETWORK_CONNECTED_ANCHOR = (
    "void Application::HandleNetworkConnectedEvent() {\n"
    "    ESP_LOGI(TAG, \"Network connected\");\n"
    "    auto state = GetDeviceState();\n"
)
_NETWORK_CONNECTED_PROFILE = (
    "void Application::HandleNetworkConnectedEvent() {\n"
    "    ESP_LOGI(TAG, \"Network connected\");\n"
    "    network_connected_ = true;\n"
    "    auto state = GetDeviceState();\n"
)
_NETWORK_DISCONNECTED_ANCHOR = (
    "void Application::HandleNetworkDisconnectedEvent() {\n"
    "    // Close current conversation when network disconnected\n"
    "    auto state = GetDeviceState();\n"
    "    if (state == kDeviceStateConnecting || state == kDeviceStateListening ||\n"
    "        state == kDeviceStateSpeaking) {\n"
    "        ESP_LOGI(TAG, \"Closing audio channel due to network disconnection\");\n"
    "        protocol_->CloseAudioChannel();\n"
    "    }\n"
)
_NETWORK_DISCONNECTED_PROFILE = (
    "void Application::HandleNetworkDisconnectedEvent() {\n"
    "    network_connected_ = false;\n"
    "    // Close both conversations and the idle control channel.\n"
    "    if (protocol_ && protocol_->IsAudioChannelOpened()) {\n"
    "        ESP_LOGI(TAG, \"Closing audio channel due to network disconnection\");\n"
    "        protocol_->CloseAudioChannel();\n"
    "    }\n"
)
_ACTIVATION_DONE_ANCHOR = (
    "    SystemInfo::PrintHeapStats();\n"
    "    SetDeviceState(kDeviceStateIdle);\n\n"
    "    has_server_time_ = ota_->HasServerTime();\n"
)
_ACTIVATION_DONE_PROFILE = (
    "    SystemInfo::PrintHeapStats();\n"
    "    SetDeviceState(kDeviceStateIdle);\n"
    "    EnsureIdleControlChannel();\n\n"
    "    has_server_time_ = ota_->HasServerTime();\n"
)
_TTS_STATE_ANCHOR = (
    "            if (strcmp(state->valuestring, \"start\") == 0) {\n"
    "                Schedule([this]() {\n"
    "                    aborted_ = false;\n"
    "                    SetDeviceState(kDeviceStateSpeaking);\n"
    "                });\n"
    "            } else if (strcmp(state->valuestring, \"stop\") == 0) {\n"
    "                Schedule([this]() {\n"
    "                    if (GetDeviceState() == kDeviceStateSpeaking) {\n"
    "                        if (listening_mode_ == kListeningModeManualStop) {\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        } else {\n"
    "                            SetDeviceState(kDeviceStateListening);\n"
    "                        }\n"
    "                    }\n"
    "                });\n"
)
_TTS_STATE_READY_PROFILE = (
    "            if (strcmp(state->valuestring, \"start\") == 0) {\n"
    "                auto purpose = cJSON_GetObjectItem(root, \"purpose\");\n"
    "                bool notification = cJSON_IsString(purpose) &&\n"
    "                    strcmp(purpose->valuestring, \"notification\") == 0;\n"
    "                Schedule([this, notification]() {\n"
    "                    aborted_ = false;\n"
    "                    notification_tts_ = notification;\n"
    "                    SetDeviceState(kDeviceStateSpeaking);\n"
    "                    if (notification && protocol_) {\n"
    "                        protocol_->SendTtsReady();\n"
    "                    }\n"
    "                });\n"
    "            } else if (strcmp(state->valuestring, \"stop\") == 0) {\n"
    "                Schedule([this]() {\n"
    "                    if (GetDeviceState() == kDeviceStateSpeaking) {\n"
    "                        if (notification_tts_ ||\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            notification_tts_ = false;\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        } else {\n"
    "                            SetDeviceState(kDeviceStateListening);\n"
    "                        }\n"
    "                    }\n"
    "                });\n"
)
_TTS_STATE_IMMEDIATE_DONE_PROFILE = _TTS_STATE_READY_PROFILE.replace(
    "                        if (notification_tts_ ||\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            notification_tts_ = false;\n"
    "                            SetDeviceState(kDeviceStateIdle);\n",
    "                        bool notification = notification_tts_;\n"
    "                        if (notification ||\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            notification_tts_ = false;\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                            if (notification && protocol_) {\n"
    "                                protocol_->SendTtsDone();\n"
    "                            }\n",
)
_TTS_STATE_CONTINUOUS_PROFILE = _TTS_STATE_READY_PROFILE.replace(
    "                    notification_tts_ = notification;\n",
    "                    notification_tts_ = notification;\n"
    "                    notification_stop_received_ = false;\n",
).replace(
    "                        if (notification_tts_ ||\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            notification_tts_ = false;\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        } else {\n"
    "                            SetDeviceState(kDeviceStateListening);\n"
    "                        }\n",
    "                        if (notification_tts_) {\n"
    "                            notification_stop_received_ = true;\n"
    "                            FinishNotificationIfPlaybackDrained();\n"
    "                        } else if (\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        } else {\n"
    "                            SetDeviceState(kDeviceStateListening);\n"
    "                        }\n",
)
_TTS_STATE_PROFILE = _TTS_STATE_CONTINUOUS_PROFILE.replace(
    "                        } else if (\n"
    "                            listening_mode_ == kListeningModeManualStop) {\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        } else {\n"
    "                            SetDeviceState(kDeviceStateListening);\n"
    "                        }\n",
    "                        } else {\n"
    "                            // Return to wake-word standby after each conversation turn.\n"
    "                            SetDeviceState(kDeviceStateIdle);\n"
    "                        }\n",
)
_APP_FIELDS_ANCHOR = (
    "    bool pending_listening_start_ = false;  // Waiting for playback to drain before starting listening (auto mode)\n"
    "    int clock_ticks_ = 0;\n"
)
_APP_FIELDS_READY_PROFILE = (
    "    bool pending_listening_start_ = false;  // Waiting for playback to drain before starting listening (auto mode)\n"
    "    bool network_connected_ = false;\n"
    "    bool notification_tts_ = false;\n"
    "    int clock_ticks_ = 0;\n"
)
_APP_FIELDS_PROFILE = _APP_FIELDS_READY_PROFILE.replace(
    "    bool notification_tts_ = false;\n",
    "    bool notification_tts_ = false;\n"
    "    bool notification_stop_received_ = false;\n",
)
_APP_METHODS_ANCHOR = (
    "    void ContinueOpenAudioChannel(ListeningMode mode);\n"
    "    void BeginWakeWordInvoke(const std::string& wake_word);\n"
)
_APP_METHODS_READY_PROFILE = (
    "    void ContinueOpenAudioChannel(ListeningMode mode);\n"
    "    void EnsureIdleControlChannel();\n"
    "    void BeginWakeWordInvoke(const std::string& wake_word);\n"
)
_APP_METHODS_PROFILE = _APP_METHODS_READY_PROFILE.replace(
    "    void EnsureIdleControlChannel();\n",
    "    void EnsureIdleControlChannel();\n"
    "    void FinishNotificationIfPlaybackDrained();\n",
)
_FINISH_NOTIFICATION_ANCHOR = (
    "void Application::EnsureIdleControlChannel() {\n"
)
_FINISH_NOTIFICATION_PROFILE = (
    "void Application::FinishNotificationIfPlaybackDrained() {\n"
    "#if CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL\n"
    "    if (!notification_tts_ || !notification_stop_received_ ||\n"
    "        !audio_service_.IsPlaybackIdle()) {\n"
    "        return;\n"
    "    }\n"
    "    notification_tts_ = false;\n"
    "    notification_stop_received_ = false;\n"
    "    SetDeviceState(kDeviceStateIdle);\n"
    "    if (protocol_) {\n"
    "        protocol_->SendTtsDone();\n"
    "    }\n"
    "#endif\n"
    "}\n\n"
    "void Application::EnsureIdleControlChannel() {\n"
)
_CONTINUE_CHANNEL_ANCHOR = "void Application::ContinueOpenAudioChannel(ListeningMode mode) {\n"
_CONTINUE_CHANNEL_PROFILE = (
    "void Application::EnsureIdleControlChannel() {\n"
    "#if CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL\n"
    "    if (!network_connected_ || GetDeviceState() != kDeviceStateIdle ||\n"
    "        !protocol_ || protocol_->IsAudioChannelOpened()) {\n"
    "        return;\n"
    "    }\n"
    "    ESP_LOGI(TAG, \"Opening idle control channel\");\n"
    "    if (protocol_->OpenAudioChannel()) {\n"
    "        Board::GetInstance().SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);\n"
    "    }\n"
    "#endif\n"
    "}\n\n"
    "void Application::ContinueOpenAudioChannel(ListeningMode mode) {\n"
)
_BUILD_IDF_ANCHOR = '    command = ["idf.py"]\n'
_BUILD_IDF_COMMAND_PROFILE = (
    '    command = [os.environ.get("XIAOYAO_IDF_COMMAND", "idf.py")]\n'
)
_BUILD_IDF_PROFILE = (
    '    idf_script = os.environ.get("XIAOYAO_IDF_SCRIPT")\n'
    "    command = (\n"
    "        [sys.executable, idf_script]\n"
    "        if idf_script\n"
    '        else [os.environ.get("XIAOYAO_IDF_COMMAND", "idf.py")]\n'
    "    )\n"
)
def _read_text_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as file:
        return file.read()


def _write_text_preserving_newlines(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(content)


def _apply_exact_profile(
    path: Path,
    anchor: str,
    replacement: str,
    *,
    previous_profiles: tuple[str, ...] = (),
) -> None:
    try:
        content = _read_text_preserving_newlines(path)
    except OSError as exc:
        raise ProfileError(f"unable to read vendor source: {path}") from exc

    line_ending = "\r\n" if "\r\n" in content else "\n"
    expected = anchor.replace("\n", line_ending)
    rendered = replacement.replace("\n", line_ending)
    if rendered in content:
        return
    for previous_profile in previous_profiles:
        previous = previous_profile.replace("\n", line_ending)
        if previous in content:
            _write_text_preserving_newlines(
                path,
                content.replace(previous, rendered, 1),
            )
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
        previous_profiles=(
            _KCONFIG_WEBSOCKET_ONLY_PROFILE,
            _KCONFIG_VAD_PROFILE,
        ),
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _PROTOCOL_ANCHOR,
        _PROTOCOL_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _SPEAKING_WAKE_WORD_ANCHOR,
        _SPEAKING_WAKE_WORD_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _VAD_CALLBACK_ANCHOR,
        _VAD_CALLBACK_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _PLAYBACK_DRAINED_ANCHOR,
        _PLAYBACK_DRAINED_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _CLOCK_TICK_ANCHOR,
        _CLOCK_TICK_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _NETWORK_CONNECTED_ANCHOR,
        _NETWORK_CONNECTED_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _NETWORK_DISCONNECTED_ANCHOR,
        _NETWORK_DISCONNECTED_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _ACTIVATION_DONE_ANCHOR,
        _ACTIVATION_DONE_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _TTS_STATE_ANCHOR,
        _TTS_STATE_PROFILE,
        previous_profiles=(
            _TTS_STATE_READY_PROFILE,
            _TTS_STATE_IMMEDIATE_DONE_PROFILE,
            _TTS_STATE_CONTINUOUS_PROFILE,
        ),
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _CONTINUE_CHANNEL_ANCHOR,
        _CONTINUE_CHANNEL_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.cc",
        _FINISH_NOTIFICATION_ANCHOR,
        _FINISH_NOTIFICATION_PROFILE,
    )
    _apply_exact_profile(
        source_root / "main" / "application.h",
        _APP_FIELDS_ANCHOR,
        _APP_FIELDS_PROFILE,
        previous_profiles=(_APP_FIELDS_READY_PROFILE,),
    )
    _apply_exact_profile(
        source_root / "main" / "application.h",
        _APP_METHODS_ANCHOR,
        _APP_METHODS_PROFILE,
        previous_profiles=(_APP_METHODS_READY_PROFILE,),
    )
    _apply_exact_profile(
        source_root / "main" / "protocols" / "protocol.h",
        _PROTOCOL_HEADER_ANCHOR,
        _PROTOCOL_HEADER_PROFILE,
        previous_profiles=(
            _PROTOCOL_HEADER_VAD_PROFILE,
            _PROTOCOL_HEADER_READY_PROFILE,
        ),
    )
    _apply_exact_profile(
        source_root / "main" / "protocols" / "protocol.cc",
        _PROTOCOL_SOURCE_ANCHOR,
        _PROTOCOL_SOURCE_PROFILE,
        previous_profiles=(
            _PROTOCOL_SOURCE_VAD_PROFILE,
            _PROTOCOL_SOURCE_READY_PROFILE,
        ),
    )
    _apply_exact_profile(
        source_root / "main" / "protocols" / "websocket_protocol.cc",
        _WEBSOCKET_FEATURE_ANCHOR,
        _WEBSOCKET_FEATURE_PROFILE,
    )
    _apply_exact_profile(
        source_root / "scripts" / "build.py",
        _BUILD_IDF_ANCHOR,
        _BUILD_IDF_PROFILE,
        previous_profiles=(_BUILD_IDF_COMMAND_PROFILE,),
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
