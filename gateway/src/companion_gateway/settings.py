import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    device_token_hashes: Mapping[str, str] = field(default_factory=dict)
    fake_voice_fixture_path: Path | None = None
    device_hello_timeout_seconds: float = 10.0
    device_audio_frame_max_bytes: int = 4096

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_path = os.environ.get("COMPANION_DB_PATH")
        configured_fixture = os.environ.get("COMPANION_FAKE_VOICE_FIXTURE_PATH")
        token_hashes_json = os.environ.get("COMPANION_DEVICE_TOKEN_HASHES", "{}")
        token_hashes = json.loads(token_hashes_json)
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
        return cls(
            database_path=(
                Path(configured_path) if configured_path else Path("data/companion.db")
            ),
            device_token_hashes={
                device_id: digest.lower()
                for device_id, digest in token_hashes.items()
            },
            fake_voice_fixture_path=(
                Path(configured_fixture) if configured_fixture else None
            ),
        )
