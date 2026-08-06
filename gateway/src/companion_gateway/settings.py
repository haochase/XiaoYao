import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_path: Path
    device_token_hashes: Mapping[str, str] = field(default_factory=dict)
    fake_voice_fixture_path: Path | None = None
    public_websocket_url: str | None = None
    ota_device_tokens: Mapping[str, str] = field(default_factory=dict)
    device_hello_timeout_seconds: float = 10.0
    device_audio_frame_max_bytes: int = 4096

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_path = os.environ.get("COMPANION_DB_PATH")
        configured_fixture = os.environ.get("COMPANION_FAKE_VOICE_FIXTURE_PATH")
        public_websocket_url = os.environ.get("COMPANION_PUBLIC_WEBSOCKET_URL")
        ota_tokens_json = os.environ.get("COMPANION_OTA_DEVICE_TOKENS", "{}")
        token_hashes_json = os.environ.get("COMPANION_DEVICE_TOKEN_HASHES", "{}")
        ota_tokens = json.loads(ota_tokens_json)
        token_hashes = json.loads(token_hashes_json)
        if not isinstance(ota_tokens, dict) or not all(
            isinstance(device_id, str)
            and bool(device_id)
            and device_id == device_id.strip()
            and len(device_id) <= 128
            and isinstance(token, str)
            and bool(token)
            and token == token.strip()
            for device_id, token in ota_tokens.items()
        ):
            raise ValueError(
                "COMPANION_OTA_DEVICE_TOKENS must map non-empty device IDs "
                "to non-empty tokens"
            )
        if not isinstance(token_hashes, dict) or not all(
            isinstance(device_id, str)
            and bool(device_id)
            and device_id == device_id.strip()
            and len(device_id) <= 128
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None
            for device_id, digest in token_hashes.items()
        ):
            raise ValueError(
                "COMPANION_DEVICE_TOKEN_HASHES must map non-empty device IDs "
                "to 64-character hexadecimal SHA-256 digests"
            )
        normalized_websocket_url = (
            public_websocket_url.strip() if public_websocket_url else None
        )
        if ota_tokens and not normalized_websocket_url:
            raise ValueError(
                "COMPANION_PUBLIC_WEBSOCKET_URL is required when OTA tokens "
                "are configured"
            )
        if normalized_websocket_url:
            parsed_url = urlparse(normalized_websocket_url)
            if (
                parsed_url.scheme not in {"ws", "wss"}
                or not parsed_url.netloc
                or parsed_url.path.rstrip("/") != "/v1/devices/ws"
            ):
                raise ValueError(
                    "COMPANION_PUBLIC_WEBSOCKET_URL must be a ws:// or wss:// "
                    "URL ending in /v1/devices/ws"
                )
        derived_hashes = {
            device_id: sha256(token.encode("utf-8")).hexdigest()
            for device_id, token in ota_tokens.items()
        }
        for device_id, derived_digest in derived_hashes.items():
            configured_digest = token_hashes.get(device_id)
            if (
                configured_digest is not None
                and configured_digest.casefold() != derived_digest
            ):
                raise ValueError(
                    "COMPANION_DEVICE_TOKEN_HASHES conflicts with OTA token"
                )
        normalized_hashes = {
            device_id: digest.lower() for device_id, digest in token_hashes.items()
        }
        normalized_hashes.update(derived_hashes)
        return cls(
            database_path=(
                Path(configured_path) if configured_path else Path("data/companion.db")
            ),
            device_token_hashes=normalized_hashes,
            fake_voice_fixture_path=(
                Path(configured_fixture) if configured_fixture else None
            ),
            public_websocket_url=normalized_websocket_url,
            ota_device_tokens=dict(ota_tokens),
        )
