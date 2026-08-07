import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


_DEVICE_WS_PATH = "/v1/devices/ws"
VoiceRuntimeMode = Literal["none", "fixture", "http", "realtime"]


def _validate_device_id(device_id: object) -> bool:
    return (
        isinstance(device_id, str)
        and bool(device_id)
        and device_id == device_id.strip()
        and len(device_id) <= 128
    )


def _normalize_ota_tokens(tokens: object) -> dict[str, str]:
    if not isinstance(tokens, Mapping) or not all(
        _validate_device_id(device_id)
        and isinstance(token, str)
        and bool(token)
        and not any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in token
        )
        for device_id, token in tokens.items()
    ):
        raise ValueError(
            "COMPANION_OTA_DEVICE_TOKENS must map non-empty device IDs "
            "to non-empty tokens without whitespace"
        )
    return dict(tokens)


def _normalize_token_hashes(token_hashes: object) -> dict[str, str]:
    if not isinstance(token_hashes, Mapping) or not all(
        _validate_device_id(device_id)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None
        for device_id, digest in token_hashes.items()
    ):
        raise ValueError(
            "COMPANION_DEVICE_TOKEN_HASHES must map non-empty device IDs "
            "to 64-character hexadecimal SHA-256 digests"
        )
    return {device_id: digest.lower() for device_id, digest in token_hashes.items()}


def _normalize_websocket_url(
    public_websocket_url: str | None,
    *,
    ota_tokens: Mapping[str, str],
) -> str | None:
    normalized_url = public_websocket_url.strip() if public_websocket_url else None
    if ota_tokens and not normalized_url:
        raise ValueError(
            "COMPANION_PUBLIC_WEBSOCKET_URL is required when OTA tokens "
            "are configured"
        )
    if not normalized_url:
        return None

    parsed_url = urlparse(normalized_url)
    try:
        hostname = parsed_url.hostname
        parsed_url.port
    except ValueError:
        hostname = None
    if (
        parsed_url.scheme not in {"ws", "wss"}
        or not hostname
        or parsed_url.path != _DEVICE_WS_PATH
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ValueError(
            "COMPANION_PUBLIC_WEBSOCKET_URL must be a ws:// or wss:// URL "
            "with the exact /v1/devices/ws path and no credentials or query"
        )
    return normalized_url


def _merge_token_hashes(
    token_hashes: Mapping[str, str],
    ota_tokens: Mapping[str, str],
) -> dict[str, str]:
    normalized_hashes = _normalize_token_hashes(token_hashes)
    derived_hashes = {
        device_id: sha256(token.encode("utf-8")).hexdigest()
        for device_id, token in ota_tokens.items()
    }
    for device_id, derived_digest in derived_hashes.items():
        configured_digest = normalized_hashes.get(device_id)
        if configured_digest is not None and configured_digest != derived_digest:
            raise ValueError(
                "COMPANION_DEVICE_TOKEN_HASHES conflicts with OTA token"
            )
    normalized_hashes.update(derived_hashes)
    return normalized_hashes


def _normalize_voice_runtime(
    configured: str,
    *,
    fixture_path: Path | None,
    minicpm_o_endpoint: str | None,
) -> VoiceRuntimeMode:
    mode = configured.strip().lower()
    if mode not in {"none", "fixture", "http", "realtime"}:
        raise ValueError(
            "COMPANION_VOICE_RUNTIME must be one of none, fixture, http, or realtime"
        )
    if mode == "fixture" and fixture_path is None:
        raise ValueError(
            "COMPANION_FAKE_VOICE_FIXTURE_PATH is required for fixture runtime"
        )
    if mode == "http" and not minicpm_o_endpoint:
        raise ValueError(
            "COMPANION_MINICPM_O_ENDPOINT is required for http runtime"
        )
    if mode == "realtime" and not minicpm_o_endpoint:
        raise ValueError(
            "COMPANION_MINICPM_O_ENDPOINT is required for realtime runtime"
        )
    if mode == "http" and urlparse(minicpm_o_endpoint).scheme not in {
        "http",
        "https",
    }:
        raise ValueError(
            "COMPANION_MINICPM_O_ENDPOINT must be HTTP for http runtime"
        )
    if mode == "realtime" and urlparse(minicpm_o_endpoint).scheme not in {
        "ws",
        "wss",
    }:
        raise ValueError(
            "COMPANION_MINICPM_O_ENDPOINT must be WebSocket for realtime runtime"
        )
    return mode  # type: ignore[return-value]


def _normalize_minicpm_o_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    normalized = endpoint.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https", "ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "COMPANION_MINICPM_O_ENDPOINT must be an HTTP or WebSocket URL "
            "without credentials"
        )
    return normalized


def _normalize_minicpm_o_auth_token(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    if not isinstance(token, str) or token != token.strip() or any(
        character.isspace()
        or ord(character) < 32
        or ord(character) == 127
        for character in token
    ):
        raise ValueError(
            "COMPANION_MINICPM_O_AUTH_TOKEN must be a non-empty token "
            "without whitespace"
        )
    return token


def _parse_bool(value: str | None, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    device_token_hashes: Mapping[str, str] = field(default_factory=dict)
    fake_voice_fixture_path: Path | None = None
    public_websocket_url: str | None = None
    ota_device_tokens: Mapping[str, str] = field(default_factory=dict)
    voice_runtime: VoiceRuntimeMode = "none"
    minicpm_o_endpoint: str | None = None
    minicpm_o_auth_token: str | None = None
    minicpm_o_timeout_seconds: float = 20.0
    task_scheduler_enabled: bool = False
    task_scheduler_interval_seconds: float = 1.0
    device_hello_timeout_seconds: float = 10.0
    device_audio_frame_max_bytes: int = 4096

    def __post_init__(self) -> None:
        ota_tokens = _normalize_ota_tokens(self.ota_device_tokens)
        normalized_url = _normalize_websocket_url(
            self.public_websocket_url,
            ota_tokens=ota_tokens,
        )
        normalized_hashes = _merge_token_hashes(
            self.device_token_hashes,
            ota_tokens,
        )
        normalized_endpoint = _normalize_minicpm_o_endpoint(self.minicpm_o_endpoint)
        normalized_auth_token = _normalize_minicpm_o_auth_token(
            self.minicpm_o_auth_token
        )
        configured_runtime = self.voice_runtime
        if configured_runtime == "none" and self.fake_voice_fixture_path is not None:
            configured_runtime = "fixture"
        normalized_runtime = _normalize_voice_runtime(
            configured_runtime,
            fixture_path=self.fake_voice_fixture_path,
            minicpm_o_endpoint=normalized_endpoint,
        )
        if self.minicpm_o_timeout_seconds <= 0:
            raise ValueError("COMPANION_MINICPM_O_TIMEOUT_SECONDS must be positive")
        if self.task_scheduler_interval_seconds <= 0:
            raise ValueError(
                "COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS must be positive"
            )
        object.__setattr__(self, "ota_device_tokens", ota_tokens)
        object.__setattr__(self, "public_websocket_url", normalized_url)
        object.__setattr__(self, "device_token_hashes", normalized_hashes)
        object.__setattr__(self, "voice_runtime", normalized_runtime)
        object.__setattr__(self, "minicpm_o_endpoint", normalized_endpoint)
        object.__setattr__(self, "minicpm_o_auth_token", normalized_auth_token)

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_path = os.environ.get("COMPANION_DB_PATH")
        configured_fixture = os.environ.get("COMPANION_FAKE_VOICE_FIXTURE_PATH")
        configured_runtime = os.environ.get("COMPANION_VOICE_RUNTIME")
        configured_endpoint = os.environ.get("COMPANION_MINICPM_O_ENDPOINT")
        configured_auth_token = os.environ.get("COMPANION_MINICPM_O_AUTH_TOKEN")
        configured_timeout = os.environ.get(
            "COMPANION_MINICPM_O_TIMEOUT_SECONDS",
            "20",
        )
        task_scheduler_enabled = _parse_bool(
            os.environ.get("COMPANION_TASK_SCHEDULER_ENABLED"),
            name="COMPANION_TASK_SCHEDULER_ENABLED",
            default=False,
        )
        configured_scheduler_interval = os.environ.get(
            "COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS",
            "1",
        )
        public_websocket_url = os.environ.get("COMPANION_PUBLIC_WEBSOCKET_URL")
        ota_tokens_json = os.environ.get("COMPANION_OTA_DEVICE_TOKENS", "{}")
        token_hashes_json = os.environ.get("COMPANION_DEVICE_TOKEN_HASHES", "{}")
        ota_tokens = json.loads(ota_tokens_json)
        token_hashes = json.loads(token_hashes_json)
        voice_runtime = (
            configured_runtime.strip().lower()
            if configured_runtime
            else ("fixture" if configured_fixture else "none")
        )
        try:
            minicpm_o_timeout_seconds = float(configured_timeout)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MINICPM_O_TIMEOUT_SECONDS must be a number"
            ) from exc
        try:
            task_scheduler_interval_seconds = float(configured_scheduler_interval)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS must be a number"
            ) from exc
        return cls(
            database_path=(
                Path(configured_path) if configured_path else Path("data/companion.db")
            ),
            device_token_hashes=token_hashes,
            fake_voice_fixture_path=(
                Path(configured_fixture) if configured_fixture else None
            ),
            public_websocket_url=public_websocket_url,
            ota_device_tokens=ota_tokens,
            voice_runtime=voice_runtime,
            minicpm_o_endpoint=configured_endpoint,
            minicpm_o_auth_token=configured_auth_token,
            minicpm_o_timeout_seconds=minicpm_o_timeout_seconds,
            task_scheduler_enabled=task_scheduler_enabled,
            task_scheduler_interval_seconds=task_scheduler_interval_seconds,
        )
