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
_DEFAULT_MIMO_OPENAI_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
_DEFAULT_MIMO_ANTHROPIC_BASE_URL = "https://token-plan-cn.xiaomimimo.com/anthropic"
VoiceRuntimeMode = Literal["none", "fixture", "http", "realtime", "mimo"]


def load_environment_file(path: Path) -> set[str]:
    """Load local defaults without overriding an explicitly exported value."""
    if not path.is_file():
        return set()

    loaded: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment assignment in {path.name}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment variable name in {path.name}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
        loaded.add(key)
    return loaded


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
    if mode not in {"none", "fixture", "http", "realtime", "mimo"}:
        raise ValueError(
            "COMPANION_VOICE_RUNTIME must be one of none, fixture, http, realtime, "
            "or mimo"
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


def _normalize_mimo_base_url(url: str, *, path: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != path
    ):
        raise ValueError(f"MiMo base URL must be an HTTP URL ending in {path}")
    return normalized.rstrip("/")


def _normalize_mimo_api_key(key: str | None) -> str | None:
    if key is None or key == "":
        return None
    if key != key.strip() or any(
        character.isspace()
        or ord(character) < 32
        or ord(character) == 127
        for character in key
    ):
        raise ValueError("COMPANION_MIMO_API_KEY must not contain whitespace")
    return key


def _parse_bool(value: str | None, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _normalize_optional_feishu_value(
    value: str | None,
    *,
    name: str,
) -> str | None:
    if value is None or value == "":
        return None
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{name} must not contain whitespace")
    return value


def _normalize_feishu_base_url(value: str) -> str:
    normalized = value.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("COMPANION_FEISHU_BASE_URL must be an HTTP URL")
    return normalized.rstrip("/")


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
    minicpm_o_max_retries: int = 2
    minicpm_o_retry_backoff_seconds: float = 1.0
    mimo_openai_base_url: str = _DEFAULT_MIMO_OPENAI_BASE_URL
    mimo_anthropic_base_url: str = _DEFAULT_MIMO_ANTHROPIC_BASE_URL
    mimo_api_key: str | None = None
    mimo_model: str = "mimo-v2.5"
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "mimo_default"
    mimo_timeout_seconds: float = 30.0
    mimo_max_retries: int = 2
    mimo_retry_backoff_seconds: float = 1.0
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_receiver_open_id: str | None = None
    feishu_base_url: str = "https://open.feishu.cn"
    feishu_timeout_seconds: float = 10.0
    feishu_max_retries: int = 2
    feishu_retry_backoff_seconds: float = 1.0
    memory_enabled: bool = False
    memory_retention_days: int = 60
    memory_quota_bytes: int = 50_000_000
    memory_proposal_ttl_seconds: int = 600
    memory_cleanup_interval_seconds: float = 86_400.0
    vision_enabled: bool = False
    vision_storage_path: Path = Path("data/vision")
    vision_max_upload_bytes: int = 10_000_000
    vision_retention_days: int = 7
    vision_quota_bytes: int = 200_000_000
    vision_cleanup_interval_seconds: float = 86_400.0
    task_scheduler_enabled: bool = False
    task_scheduler_interval_seconds: float = 1.0
    device_hello_timeout_seconds: float = 10.0
    device_audio_frame_max_bytes: int = 4096
    audio_queue_capacity: int = 256

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
        normalized_mimo_openai_base_url = _normalize_mimo_base_url(
            self.mimo_openai_base_url,
            path="/v1",
        )
        normalized_mimo_anthropic_base_url = _normalize_mimo_base_url(
            self.mimo_anthropic_base_url,
            path="/anthropic",
        )
        normalized_mimo_api_key = _normalize_mimo_api_key(self.mimo_api_key)
        normalized_feishu_app_id = _normalize_optional_feishu_value(
            self.feishu_app_id,
            name="COMPANION_FEISHU_APP_ID",
        )
        normalized_feishu_app_secret = _normalize_optional_feishu_value(
            self.feishu_app_secret,
            name="COMPANION_FEISHU_APP_SECRET",
        )
        normalized_feishu_receiver_open_id = _normalize_optional_feishu_value(
            self.feishu_receiver_open_id,
            name="COMPANION_FEISHU_RECEIVER_OPEN_ID",
        )
        feishu_values = (
            normalized_feishu_app_id,
            normalized_feishu_app_secret,
            normalized_feishu_receiver_open_id,
        )
        if any(value is not None for value in feishu_values) and not all(
            value is not None for value in feishu_values
        ):
            raise ValueError(
                "COMPANION_FEISHU_APP_ID, COMPANION_FEISHU_APP_SECRET, and "
                "COMPANION_FEISHU_RECEIVER_OPEN_ID must be configured together"
            )
        normalized_feishu_base_url = _normalize_feishu_base_url(self.feishu_base_url)
        configured_runtime = self.voice_runtime
        if configured_runtime == "none" and self.fake_voice_fixture_path is not None:
            configured_runtime = "fixture"
        normalized_runtime = _normalize_voice_runtime(
            configured_runtime,
            fixture_path=self.fake_voice_fixture_path,
            minicpm_o_endpoint=normalized_endpoint,
        )
        if normalized_runtime == "mimo" and normalized_mimo_api_key is None:
            raise ValueError("COMPANION_MIMO_API_KEY is required for mimo runtime")
        if self.minicpm_o_timeout_seconds <= 0:
            raise ValueError("COMPANION_MINICPM_O_TIMEOUT_SECONDS must be positive")
        if self.minicpm_o_max_retries < 0:
            raise ValueError("COMPANION_MINICPM_O_MAX_RETRIES must not be negative")
        if self.minicpm_o_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if self.mimo_timeout_seconds <= 0:
            raise ValueError("COMPANION_MIMO_TIMEOUT_SECONDS must be positive")
        if self.mimo_max_retries < 0:
            raise ValueError("COMPANION_MIMO_MAX_RETRIES must not be negative")
        if self.mimo_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_MIMO_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if self.feishu_timeout_seconds <= 0:
            raise ValueError("COMPANION_FEISHU_TIMEOUT_SECONDS must be positive")
        if self.feishu_max_retries < 0:
            raise ValueError("COMPANION_FEISHU_MAX_RETRIES must not be negative")
        if self.feishu_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_FEISHU_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if not isinstance(self.memory_enabled, bool):
            raise ValueError("COMPANION_MEMORY_ENABLED must be true or false")
        if self.memory_retention_days <= 0:
            raise ValueError("COMPANION_MEMORY_RETENTION_DAYS must be positive")
        if self.memory_quota_bytes <= 0:
            raise ValueError("COMPANION_MEMORY_QUOTA_BYTES must be positive")
        if self.memory_proposal_ttl_seconds <= 0:
            raise ValueError(
                "COMPANION_MEMORY_PROPOSAL_TTL_SECONDS must be positive"
            )
        if self.memory_cleanup_interval_seconds <= 0:
            raise ValueError(
                "COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS must be positive"
            )
        if not isinstance(self.vision_enabled, bool):
            raise ValueError("COMPANION_VISION_ENABLED must be true or false")
        if self.vision_max_upload_bytes <= 0 or self.vision_max_upload_bytes > 10_000_000:
            raise ValueError(
                "COMPANION_VISION_MAX_UPLOAD_BYTES must be between 1 and 10000000"
            )
        if self.vision_retention_days <= 0:
            raise ValueError("COMPANION_VISION_RETENTION_DAYS must be positive")
        if self.vision_quota_bytes <= 0:
            raise ValueError("COMPANION_VISION_QUOTA_BYTES must be positive")
        if self.vision_cleanup_interval_seconds <= 0:
            raise ValueError(
                "COMPANION_VISION_CLEANUP_INTERVAL_SECONDS must be positive"
            )
        if self.audio_queue_capacity < 1:
            raise ValueError("COMPANION_AUDIO_QUEUE_CAPACITY must be positive")
        for value, name in (
            (self.mimo_model, "COMPANION_MIMO_MODEL"),
            (self.mimo_tts_model, "COMPANION_MIMO_TTS_MODEL"),
            (self.mimo_tts_voice, "COMPANION_MIMO_TTS_VOICE"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
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
        object.__setattr__(
            self,
            "mimo_openai_base_url",
            normalized_mimo_openai_base_url,
        )
        object.__setattr__(
            self,
            "mimo_anthropic_base_url",
            normalized_mimo_anthropic_base_url,
        )
        object.__setattr__(self, "mimo_api_key", normalized_mimo_api_key)
        object.__setattr__(self, "feishu_app_id", normalized_feishu_app_id)
        object.__setattr__(self, "feishu_app_secret", normalized_feishu_app_secret)
        object.__setattr__(
            self,
            "feishu_receiver_open_id",
            normalized_feishu_receiver_open_id,
        )
        object.__setattr__(self, "feishu_base_url", normalized_feishu_base_url)

    @property
    def feishu_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self.feishu_app_id,
                self.feishu_app_secret,
                self.feishu_receiver_open_id,
            )
        )

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_path = os.environ.get("COMPANION_DB_PATH")
        configured_fixture = os.environ.get("COMPANION_FAKE_VOICE_FIXTURE_PATH")
        configured_runtime = os.environ.get("COMPANION_VOICE_RUNTIME")
        configured_endpoint = os.environ.get("COMPANION_MINICPM_O_ENDPOINT")
        configured_auth_token = os.environ.get("COMPANION_MINICPM_O_AUTH_TOKEN")
        configured_mimo_openai_base_url = os.environ.get(
            "COMPANION_MIMO_OPENAI_BASE_URL",
            _DEFAULT_MIMO_OPENAI_BASE_URL,
        )
        configured_mimo_anthropic_base_url = os.environ.get(
            "COMPANION_MIMO_ANTHROPIC_BASE_URL",
            _DEFAULT_MIMO_ANTHROPIC_BASE_URL,
        )
        configured_mimo_api_key = os.environ.get("COMPANION_MIMO_API_KEY")
        configured_mimo_model = os.environ.get(
            "COMPANION_MIMO_MODEL",
            "mimo-v2.5",
        )
        configured_mimo_tts_model = os.environ.get(
            "COMPANION_MIMO_TTS_MODEL",
            "mimo-v2.5-tts",
        )
        configured_mimo_tts_voice = os.environ.get(
            "COMPANION_MIMO_TTS_VOICE",
            "mimo_default",
        )
        configured_mimo_timeout = os.environ.get(
            "COMPANION_MIMO_TIMEOUT_SECONDS",
            "30",
        )
        configured_mimo_max_retries = os.environ.get(
            "COMPANION_MIMO_MAX_RETRIES",
            "2",
        )
        configured_mimo_retry_backoff = os.environ.get(
            "COMPANION_MIMO_RETRY_BACKOFF_SECONDS",
            "1",
        )
        configured_feishu_app_id = os.environ.get("COMPANION_FEISHU_APP_ID")
        configured_feishu_app_secret = os.environ.get("COMPANION_FEISHU_APP_SECRET")
        configured_feishu_receiver_open_id = os.environ.get(
            "COMPANION_FEISHU_RECEIVER_OPEN_ID"
        )
        configured_feishu_base_url = os.environ.get(
            "COMPANION_FEISHU_BASE_URL",
            "https://open.feishu.cn",
        )
        configured_feishu_timeout = os.environ.get(
            "COMPANION_FEISHU_TIMEOUT_SECONDS",
            "10",
        )
        configured_feishu_max_retries = os.environ.get(
            "COMPANION_FEISHU_MAX_RETRIES",
            "2",
        )
        configured_feishu_retry_backoff = os.environ.get(
            "COMPANION_FEISHU_RETRY_BACKOFF_SECONDS",
            "1",
        )
        configured_timeout = os.environ.get(
            "COMPANION_MINICPM_O_TIMEOUT_SECONDS",
            "20",
        )
        configured_minicpm_max_retries = os.environ.get(
            "COMPANION_MINICPM_O_MAX_RETRIES",
            "2",
        )
        configured_minicpm_retry_backoff = os.environ.get(
            "COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS",
            "1",
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
        memory_enabled = _parse_bool(
            os.environ.get("COMPANION_MEMORY_ENABLED"),
            name="COMPANION_MEMORY_ENABLED",
            default=False,
        )
        configured_memory_retention_days = os.environ.get(
            "COMPANION_MEMORY_RETENTION_DAYS",
            "60",
        )
        configured_memory_quota_bytes = os.environ.get(
            "COMPANION_MEMORY_QUOTA_BYTES",
            "50000000",
        )
        configured_memory_proposal_ttl = os.environ.get(
            "COMPANION_MEMORY_PROPOSAL_TTL_SECONDS",
            "600",
        )
        configured_memory_cleanup_interval = os.environ.get(
            "COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS",
            "86400",
        )
        vision_enabled = _parse_bool(
            os.environ.get("COMPANION_VISION_ENABLED"),
            name="COMPANION_VISION_ENABLED",
            default=False,
        )
        configured_vision_storage_path = os.environ.get(
            "COMPANION_VISION_STORAGE_PATH",
            "data/vision",
        )
        configured_vision_max_upload_bytes = os.environ.get(
            "COMPANION_VISION_MAX_UPLOAD_BYTES",
            "10000000",
        )
        configured_vision_retention_days = os.environ.get(
            "COMPANION_VISION_RETENTION_DAYS",
            "7",
        )
        configured_vision_quota_bytes = os.environ.get(
            "COMPANION_VISION_QUOTA_BYTES",
            "200000000",
        )
        configured_vision_cleanup_interval = os.environ.get(
            "COMPANION_VISION_CLEANUP_INTERVAL_SECONDS",
            "86400",
        )
        configured_audio_queue_capacity = os.environ.get(
            "COMPANION_AUDIO_QUEUE_CAPACITY",
            "256",
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
            minicpm_o_max_retries = int(configured_minicpm_max_retries)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MINICPM_O_MAX_RETRIES must be an integer"
            ) from exc
        try:
            minicpm_o_retry_backoff_seconds = float(configured_minicpm_retry_backoff)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS must be a number"
            ) from exc
        try:
            task_scheduler_interval_seconds = float(configured_scheduler_interval)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS must be a number"
            ) from exc
        try:
            mimo_timeout_seconds = float(configured_mimo_timeout)
        except ValueError as exc:
            raise ValueError("COMPANION_MIMO_TIMEOUT_SECONDS must be a number") from exc
        try:
            mimo_max_retries = int(configured_mimo_max_retries)
        except ValueError as exc:
            raise ValueError("COMPANION_MIMO_MAX_RETRIES must be an integer") from exc
        try:
            mimo_retry_backoff_seconds = float(configured_mimo_retry_backoff)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MIMO_RETRY_BACKOFF_SECONDS must be a number"
            ) from exc
        try:
            feishu_timeout_seconds = float(configured_feishu_timeout)
        except ValueError as exc:
            raise ValueError("COMPANION_FEISHU_TIMEOUT_SECONDS must be a number") from exc
        try:
            feishu_max_retries = int(configured_feishu_max_retries)
        except ValueError as exc:
            raise ValueError("COMPANION_FEISHU_MAX_RETRIES must be an integer") from exc
        try:
            feishu_retry_backoff_seconds = float(configured_feishu_retry_backoff)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_FEISHU_RETRY_BACKOFF_SECONDS must be a number"
            ) from exc
        try:
            audio_queue_capacity = int(configured_audio_queue_capacity)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_AUDIO_QUEUE_CAPACITY must be an integer"
            ) from exc
        try:
            memory_retention_days = int(configured_memory_retention_days)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MEMORY_RETENTION_DAYS must be an integer"
            ) from exc
        try:
            memory_quota_bytes = int(configured_memory_quota_bytes)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MEMORY_QUOTA_BYTES must be an integer"
            ) from exc
        try:
            memory_proposal_ttl_seconds = int(configured_memory_proposal_ttl)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MEMORY_PROPOSAL_TTL_SECONDS must be an integer"
            ) from exc
        try:
            memory_cleanup_interval_seconds = float(
                configured_memory_cleanup_interval
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MEMORY_CLEANUP_INTERVAL_SECONDS must be a number"
            ) from exc
        try:
            vision_max_upload_bytes = int(configured_vision_max_upload_bytes)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_VISION_MAX_UPLOAD_BYTES must be an integer"
            ) from exc
        try:
            vision_retention_days = int(configured_vision_retention_days)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_VISION_RETENTION_DAYS must be an integer"
            ) from exc
        try:
            vision_quota_bytes = int(configured_vision_quota_bytes)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_VISION_QUOTA_BYTES must be an integer"
            ) from exc
        try:
            vision_cleanup_interval_seconds = float(
                configured_vision_cleanup_interval
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_VISION_CLEANUP_INTERVAL_SECONDS must be a number"
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
            minicpm_o_max_retries=minicpm_o_max_retries,
            minicpm_o_retry_backoff_seconds=minicpm_o_retry_backoff_seconds,
            mimo_openai_base_url=configured_mimo_openai_base_url,
            mimo_anthropic_base_url=configured_mimo_anthropic_base_url,
            mimo_api_key=configured_mimo_api_key,
            mimo_model=configured_mimo_model,
            mimo_tts_model=configured_mimo_tts_model,
            mimo_tts_voice=configured_mimo_tts_voice,
            mimo_timeout_seconds=mimo_timeout_seconds,
            mimo_max_retries=mimo_max_retries,
            mimo_retry_backoff_seconds=mimo_retry_backoff_seconds,
            feishu_app_id=configured_feishu_app_id,
            feishu_app_secret=configured_feishu_app_secret,
            feishu_receiver_open_id=configured_feishu_receiver_open_id,
            feishu_base_url=configured_feishu_base_url,
            feishu_timeout_seconds=feishu_timeout_seconds,
            feishu_max_retries=feishu_max_retries,
            feishu_retry_backoff_seconds=feishu_retry_backoff_seconds,
            memory_enabled=memory_enabled,
            memory_retention_days=memory_retention_days,
            memory_quota_bytes=memory_quota_bytes,
            memory_proposal_ttl_seconds=memory_proposal_ttl_seconds,
            memory_cleanup_interval_seconds=memory_cleanup_interval_seconds,
            vision_enabled=vision_enabled,
            vision_storage_path=Path(configured_vision_storage_path),
            vision_max_upload_bytes=vision_max_upload_bytes,
            vision_retention_days=vision_retention_days,
            vision_quota_bytes=vision_quota_bytes,
            vision_cleanup_interval_seconds=vision_cleanup_interval_seconds,
            task_scheduler_enabled=task_scheduler_enabled,
            task_scheduler_interval_seconds=task_scheduler_interval_seconds,
            audio_queue_capacity=audio_queue_capacity,
        )
