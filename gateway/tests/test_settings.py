from hashlib import sha256
from pathlib import Path

import pytest

from companion_gateway.settings import Settings


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
