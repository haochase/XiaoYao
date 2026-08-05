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
