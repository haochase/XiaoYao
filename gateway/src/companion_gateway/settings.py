import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse


_DEVICE_WS_PATH = "/v1/devices/ws"


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


@dataclass(frozen=True)
class Settings:
    database_path: Path
    device_token_hashes: Mapping[str, str] = field(default_factory=dict)
    fake_voice_fixture_path: Path | None = None
    public_websocket_url: str | None = None
    ota_device_tokens: Mapping[str, str] = field(default_factory=dict)
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
        object.__setattr__(self, "ota_device_tokens", ota_tokens)
        object.__setattr__(self, "public_websocket_url", normalized_url)
        object.__setattr__(self, "device_token_hashes", normalized_hashes)

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_path = os.environ.get("COMPANION_DB_PATH")
        configured_fixture = os.environ.get("COMPANION_FAKE_VOICE_FIXTURE_PATH")
        public_websocket_url = os.environ.get("COMPANION_PUBLIC_WEBSOCKET_URL")
        ota_tokens_json = os.environ.get("COMPANION_OTA_DEVICE_TOKENS", "{}")
        token_hashes_json = os.environ.get("COMPANION_DEVICE_TOKEN_HASHES", "{}")
        ota_tokens = json.loads(ota_tokens_json)
        token_hashes = json.loads(token_hashes_json)
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
        )
