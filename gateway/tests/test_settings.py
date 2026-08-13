from hashlib import sha256
import os
from pathlib import Path

import pytest

from companion_gateway.settings import Settings, load_environment_file


def test_load_environment_file_uses_file_defaults_and_preserves_process_values(
    tmp_path,
    monkeypatch,
) -> None:
    environment_file = tmp_path / ".env"
    environment_file.write_text(
        "# local-only settings\n"
        "COMPANION_VOICE_RUNTIME=mimo\n"
        "COMPANION_MIMO_API_KEY='file-token'\n"
        "EMPTY_VALUE=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "fixture")
    monkeypatch.delenv("COMPANION_MIMO_API_KEY", raising=False)

    loaded = load_environment_file(environment_file)

    assert loaded == {
        "COMPANION_VOICE_RUNTIME",
        "COMPANION_MIMO_API_KEY",
        "EMPTY_VALUE",
    }
    assert os.environ["COMPANION_VOICE_RUNTIME"] == "fixture"
    assert os.environ["COMPANION_MIMO_API_KEY"] == "file-token"
    assert os.environ["EMPTY_VALUE"] == ""


def test_settings_parse_valid_device_token_hashes(monkeypatch) -> None:
    digest = "a" * 64
    monkeypatch.setenv("COMPANION_DB_PATH", "data/test-settings.db")
    monkeypatch.setenv(
        "COMPANION_DEVICE_TOKEN_HASHES",
        f'{{"dev-test":"{digest}"}}',
    )

    settings = Settings.from_environment()

    assert settings.database_path == Path("data/test-settings.db")
    assert settings.device_token_hashes == {"dev-test": digest}


def test_settings_parse_optional_fake_voice_fixture_path(monkeypatch) -> None:
    monkeypatch.setenv(
        "COMPANION_FAKE_VOICE_FIXTURE_PATH",
        "../assets/audio/companion-greeting-zh-cn.wav",
    )

    settings = Settings.from_environment()

    assert settings.fake_voice_fixture_path == Path(
        "../assets/audio/companion-greeting-zh-cn.wav"
    )


def test_settings_selects_http_voice_runtime(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "http")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "http://127.0.0.1:9000/v1/infer",
    )

    settings = Settings.from_environment()

    assert settings.voice_runtime == "http"
    assert settings.minicpm_o_endpoint == "http://127.0.0.1:9000/v1/infer"


def test_settings_selects_realtime_voice_runtime(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "realtime")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "wss://minicpm.example.test/v1/realtime?mode=audio",
    )

    settings = Settings.from_environment()

    assert settings.voice_runtime == "realtime"


def test_settings_loads_minicpm_retry_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MINICPM_O_MAX_RETRIES", "4")
    monkeypatch.setenv("COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS", "0.25")

    settings = Settings.from_environment()

    assert settings.minicpm_o_max_retries == 4
    assert settings.minicpm_o_retry_backoff_seconds == 0.25


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("minicpm_o_max_retries", -1, "MAX_RETRIES"),
        ("minicpm_o_retry_backoff_seconds", -0.1, "RETRY_BACKOFF_SECONDS"),
    ],
)
def test_settings_rejects_negative_minicpm_retry_values(
    field: str, value: float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(database_path=Path("data/test.db"), **{field: value})


def test_settings_loads_mimo_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "mimo")
    monkeypatch.setenv("COMPANION_MIMO_API_KEY", "example-token")
    monkeypatch.setenv("COMPANION_AUDIO_QUEUE_CAPACITY", "96")
    monkeypatch.setenv("COMPANION_MIMO_MAX_RETRIES", "3")
    monkeypatch.setenv("COMPANION_MIMO_RETRY_BACKOFF_SECONDS", "0.25")

    settings = Settings.from_environment()

    assert settings.voice_runtime == "mimo"
    assert settings.mimo_openai_base_url == (
        "https://token-plan-cn.xiaomimimo.com/v1"
    )
    assert settings.mimo_anthropic_base_url == (
        "https://token-plan-cn.xiaomimimo.com/anthropic"
    )
    assert settings.mimo_api_key == "example-token"
    assert settings.audio_queue_capacity == 96
    assert settings.mimo_max_retries == 3
    assert settings.mimo_retry_backoff_seconds == 0.25


def test_settings_loads_device_auto_stop_idle_seconds(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS", "1.5")

    settings = Settings.from_environment()

    assert settings.device_auto_stop_idle_seconds == 1.5


def test_settings_loads_device_auto_turn_pcm_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD", "35")
    monkeypatch.setenv("COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES", "12")
    monkeypatch.setenv("COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES", "5")
    monkeypatch.setenv("COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES", "5")

    settings = Settings.from_environment()

    assert settings.device_auto_turn_rms_threshold == 35.0
    assert settings.device_auto_turn_silence_frames == 12
    assert settings.device_auto_turn_min_speech_frames == 5
    assert settings.device_post_tts_silence_frames == 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD", "-1"),
        ("COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD", "not-a-number"),
        ("COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES", "0"),
        ("COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES", "not-a-number"),
        ("COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES", "0"),
        ("COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES", "not-a-number"),
        ("COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES", "0"),
        ("COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES", "not-a-number"),
    ],
)
def test_settings_rejects_invalid_device_auto_turn_pcm_endpoint(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_environment()


@pytest.mark.parametrize("value", ["0", "-0.1", "not-a-number"])
def test_settings_rejects_invalid_device_auto_stop_idle_seconds(
    monkeypatch,
    value: str,
) -> None:
    monkeypatch.setenv("COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS", value)

    with pytest.raises(ValueError, match="COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS"):
        Settings.from_environment()


def test_settings_requires_mimo_api_key(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "mimo")
    monkeypatch.delenv("COMPANION_MIMO_API_KEY", raising=False)

    with pytest.raises(ValueError, match="COMPANION_MIMO_API_KEY"):
        Settings.from_environment()


def test_settings_loads_minicpm_o_auth_token(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MINICPM_O_AUTH_TOKEN", "ascend-runtime-token")

    settings = Settings.from_environment()

    assert settings.minicpm_o_auth_token == "ascend-runtime-token"


def test_settings_rejects_minicpm_o_auth_token_with_whitespace(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MINICPM_O_AUTH_TOKEN", "token with spaces")

    with pytest.raises(ValueError, match="COMPANION_MINICPM_O_AUTH_TOKEN"):
        Settings.from_environment()


def test_settings_parse_task_scheduler_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_TASK_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS", "2.5")

    settings = Settings.from_environment()

    assert settings.task_scheduler_enabled is True
    assert settings.task_scheduler_interval_seconds == 2.5


def test_settings_loads_feishu_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_FEISHU_APP_ID", "cli_test_app")
    monkeypatch.setenv("COMPANION_FEISHU_APP_SECRET", "secret_test_value")
    monkeypatch.setenv("COMPANION_FEISHU_RECEIVER_OPEN_ID", "ou_test_receiver")
    monkeypatch.setenv("COMPANION_FEISHU_MAX_RETRIES", "3")
    monkeypatch.setenv("COMPANION_FEISHU_RETRY_BACKOFF_SECONDS", "0.25")

    settings = Settings.from_environment()

    assert settings.feishu_configured is True
    assert settings.feishu_app_id == "cli_test_app"
    assert settings.feishu_receiver_open_id == "ou_test_receiver"
    assert settings.feishu_max_retries == 3
    assert settings.feishu_retry_backoff_seconds == 0.25


def test_settings_loads_memory_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MEMORY_ENABLED", "true")
    monkeypatch.setenv("COMPANION_MEMORY_RETENTION_DAYS", "45")
    monkeypatch.setenv("COMPANION_MEMORY_QUOTA_BYTES", "1234")

    settings = Settings.from_environment()

    assert settings.memory_enabled is True
    assert settings.memory_retention_days == 45
    assert settings.memory_quota_bytes == 1234


def test_settings_loads_memory_proposal_ttl(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MEMORY_PROPOSAL_TTL_SECONDS", "90")

    settings = Settings.from_environment()

    assert settings.memory_proposal_ttl_seconds == 90


def test_settings_loads_memory_cleanup_interval(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS", "3600")

    settings = Settings.from_environment()

    assert settings.memory_cleanup_interval_seconds == 3600


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMPANION_MEMORY_ENABLED", "sometimes"),
        ("COMPANION_MEMORY_RETENTION_DAYS", "0"),
        ("COMPANION_MEMORY_QUOTA_BYTES", "0"),
        ("COMPANION_MEMORY_PROPOSAL_TTL_SECONDS", "0"),
        ("COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS", "0"),
    ],
)
def test_settings_rejects_invalid_memory_configuration(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_environment()


def test_settings_loads_vision_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COMPANION_VISION_ENABLED", "true")
    monkeypatch.setenv("COMPANION_VISION_STORAGE_PATH", str(tmp_path / "vision"))
    monkeypatch.setenv("COMPANION_VISION_MAX_UPLOAD_BYTES", "9000000")
    monkeypatch.setenv("COMPANION_VISION_RETENTION_DAYS", "7")
    monkeypatch.setenv("COMPANION_VISION_QUOTA_BYTES", "123456")
    monkeypatch.setenv("COMPANION_VISION_CLEANUP_INTERVAL_SECONDS", "3600")

    settings = Settings.from_environment()

    assert settings.vision_enabled is True
    assert settings.vision_storage_path == tmp_path / "vision"
    assert settings.vision_max_upload_bytes == 9_000_000
    assert settings.vision_retention_days == 7
    assert settings.vision_quota_bytes == 123456
    assert settings.vision_cleanup_interval_seconds == 3600


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMPANION_VISION_ENABLED", "sometimes"),
        ("COMPANION_VISION_MAX_UPLOAD_BYTES", "0"),
        ("COMPANION_VISION_RETENTION_DAYS", "0"),
        ("COMPANION_VISION_QUOTA_BYTES", "0"),
        ("COMPANION_VISION_CLEANUP_INTERVAL_SECONDS", "0"),
    ],
)
def test_settings_rejects_invalid_vision_configuration(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_environment()


def test_settings_rejects_partial_feishu_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_FEISHU_APP_ID", "cli_test_app")
    monkeypatch.delenv("COMPANION_FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("COMPANION_FEISHU_RECEIVER_OPEN_ID", raising=False)

    with pytest.raises(ValueError, match="COMPANION_FEISHU"):
        Settings.from_environment()


def test_settings_reject_invalid_task_scheduler_configuration(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_TASK_SCHEDULER_ENABLED", "sometimes")

    with pytest.raises(ValueError, match="COMPANION_TASK_SCHEDULER_ENABLED"):
        Settings.from_environment()


def test_settings_requires_endpoint_for_http_runtime(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "http")
    monkeypatch.delenv("COMPANION_MINICPM_O_ENDPOINT", raising=False)

    with pytest.raises(ValueError, match="COMPANION_MINICPM_O_ENDPOINT"):
        Settings.from_environment()


def test_settings_rejects_unknown_voice_runtime(monkeypatch) -> None:
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "unknown")

    with pytest.raises(ValueError, match="COMPANION_VOICE_RUNTIME"):
        Settings.from_environment()


def test_settings_derives_a_device_digest_from_ota_token(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "gateway.db"))
    monkeypatch.setenv(
        "COMPANION_PUBLIC_WEBSOCKET_URL",
        "ws://192.0.2.10:8723/v1/devices/ws",
    )
    monkeypatch.setenv(
        "COMPANION_OTA_DEVICE_TOKENS",
        '{"device-test":"bootstrap-token"}',
    )

    settings = Settings.from_environment()

    assert settings.ota_device_tokens == {"device-test": "bootstrap-token"}
    assert settings.device_token_hashes["device-test"] == sha256(
        b"bootstrap-token"
    ).hexdigest()


@pytest.mark.parametrize(
    "public_url,ota_tokens,device_hashes",
    [
        (
            "http://192.0.2.10:8723/v1/devices/ws",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/other",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://user:password@192.0.2.10:8723/v1/devices/ws",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws?token=secret",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws#secret",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws/",
            '{"device-test":"bootstrap-token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws",
            '{"device-test":""}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws",
            '{"device-test":"bootstrap token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws",
            '{"device-test":"Bearer token"}',
            "{}",
        ),
        (
            "ws://192.0.2.10:8723/v1/devices/ws",
            '{"device-test":"bootstrap-token"}',
            '{"device-test":"' + ("a" * 64) + '"}',
        ),
    ],
)
def test_settings_reject_invalid_ota_configuration(
    monkeypatch,
    public_url: str,
    ota_tokens: str,
    device_hashes: str,
) -> None:
    monkeypatch.setenv("COMPANION_PUBLIC_WEBSOCKET_URL", public_url)
    monkeypatch.setenv("COMPANION_OTA_DEVICE_TOKENS", ota_tokens)
    monkeypatch.setenv("COMPANION_DEVICE_TOKEN_HASHES", device_hashes)

    with pytest.raises(ValueError, match="(?i)ota|websocket|token"):
        Settings.from_environment()


def test_settings_reject_ota_tokens_without_a_public_url(monkeypatch) -> None:
    monkeypatch.delenv("COMPANION_PUBLIC_WEBSOCKET_URL", raising=False)
    monkeypatch.setenv(
        "COMPANION_OTA_DEVICE_TOKENS",
        '{"device-test":"bootstrap-token"}',
    )

    with pytest.raises(ValueError, match="COMPANION_PUBLIC_WEBSOCKET_URL"):
        Settings.from_environment()


def test_direct_settings_configuration_derives_device_digest(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "direct.db",
        public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
        ota_device_tokens={"device-test": "bootstrap-token"},
    )

    assert settings.device_token_hashes == {
        "device-test": sha256(b"bootstrap-token").hexdigest()
    }


def test_direct_settings_configuration_rejects_conflicting_digest(tmp_path) -> None:
    with pytest.raises(ValueError, match="conflicts"):
        Settings(
            database_path=tmp_path / "direct-conflict.db",
            public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
            ota_device_tokens={"device-test": "bootstrap-token"},
            device_token_hashes={"device-test": "a" * 64},
        )


@pytest.mark.parametrize(
    "configured",
    [
        "[]",
        '{"":"' + ("a" * 64) + '"}',
        '{"dev-test":"short"}',
        '{"dev-test":"' + ("z" * 64) + '"}',
    ],
)
def test_settings_reject_invalid_device_token_hashes(
    monkeypatch,
    configured: str,
) -> None:
    monkeypatch.setenv("COMPANION_DEVICE_TOKEN_HASHES", configured)

    with pytest.raises(ValueError, match="COMPANION_DEVICE_TOKEN_HASHES"):
        Settings.from_environment()
