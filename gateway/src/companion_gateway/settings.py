import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


_DEVICE_WS_PATH = "/v1/devices/ws"
_DEFAULT_MINICPM_O_COMPATIBLE_BASE_URL = "http://127.0.0.1:9000/v1"
VoiceRuntimeMode = Literal["none", "fixture", "http", "realtime"]


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


def _normalize_minicpm_o_base_url(url: str, *, path: str) -> str:
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
        raise ValueError(f"MiniCPM-o 4.5 base URL must be an HTTP URL ending in {path}")
    return normalized.rstrip("/")


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
    minicpm_o_compatible_base_url: str = _DEFAULT_MINICPM_O_COMPATIBLE_BASE_URL
    minicpm_o_model: str = "MiniCPM-O-4.5-9B"
    minicpm_o_compatible_timeout_seconds: float = 30.0
    minicpm_o_compatible_max_retries: int = 2
    minicpm_o_compatible_retry_backoff_seconds: float = 1.0
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_receiver_open_id: str | None = None
    feishu_base_url: str = "https://open.feishu.cn"
    feishu_timeout_seconds: float = 10.0
    feishu_max_retries: int = 2
    feishu_retry_backoff_seconds: float = 1.0
    feishu_chat_enabled: bool = False
    feishu_chat_history_turns: int = 6
    feishu_chat_startup_timeout_seconds: float = 10.0
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
    camera_enabled: bool = False
    camera_max_bytes: int = 2_097_152
    task_scheduler_enabled: bool = False
    task_scheduler_interval_seconds: float = 1.0
    dynamic_agents_enabled: bool = False
    dynamic_agent_owner_id: str | None = None
    dynamic_agent_target_device_id: str | None = None
    dynamic_agent_scheduler_interval_seconds: float = 1.0
    device_conversation_idle_timeout_seconds: float = 15.0
    device_continuous_conversation_enabled: bool = False
    device_control_keepalive_seconds: float = 60.0
    recent_context_enabled: bool = False
    recent_context_retention_days: int = 7
    recent_context_max_messages: int = 20
    recent_context_max_bytes: int = 4096
    subject_id: str = "voice-user"
    device_hello_timeout_seconds: float = 10.0
    device_audio_frame_max_bytes: int = 4096
    device_auto_stop_idle_seconds: float = 1.2
    device_auto_turn_rms_threshold: float | None = None
    device_vad_turn_rms_threshold: float | None = None
    device_auto_turn_silence_frames: int = 12
    device_auto_turn_min_speech_frames: int = 5
    device_auto_turn_max_frames: int = 150
    device_vad_post_tts_rms_threshold: float = 35.0
    device_post_tts_silence_frames: int = 3
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
        normalized_minicpm_o_compatible_base_url = _normalize_minicpm_o_base_url(
            self.minicpm_o_compatible_base_url,
            path="/v1",
        )
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
        normalized_dynamic_agent_owner_id = _normalize_optional_feishu_value(
            self.dynamic_agent_owner_id,
            name="COMPANION_DYNAMIC_AGENT_OWNER_ID",
        )
        normalized_dynamic_agent_target_device_id = (
            _normalize_optional_feishu_value(
                self.dynamic_agent_target_device_id,
                name="COMPANION_DYNAMIC_AGENT_TARGET_DEVICE_ID",
            )
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
        if self.minicpm_o_timeout_seconds <= 0:
            raise ValueError("COMPANION_MINICPM_O_TIMEOUT_SECONDS must be positive")
        if self.minicpm_o_max_retries < 0:
            raise ValueError("COMPANION_MINICPM_O_MAX_RETRIES must not be negative")
        if self.minicpm_o_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if self.minicpm_o_compatible_timeout_seconds <= 0:
            raise ValueError("COMPANION_MINICPM_O_COMPATIBLE_TIMEOUT_SECONDS must be positive")
        if self.minicpm_o_compatible_max_retries < 0:
            raise ValueError("COMPANION_MINICPM_O_COMPATIBLE_MAX_RETRIES must not be negative")
        if self.minicpm_o_compatible_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_MINICPM_O_COMPATIBLE_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if self.feishu_timeout_seconds <= 0:
            raise ValueError("COMPANION_FEISHU_TIMEOUT_SECONDS must be positive")
        if self.feishu_max_retries < 0:
            raise ValueError("COMPANION_FEISHU_MAX_RETRIES must not be negative")
        if self.feishu_retry_backoff_seconds < 0:
            raise ValueError(
                "COMPANION_FEISHU_RETRY_BACKOFF_SECONDS must not be negative"
            )
        if not isinstance(self.feishu_chat_enabled, bool):
            raise ValueError("COMPANION_FEISHU_CHAT_ENABLED must be true or false")
        if self.feishu_chat_history_turns < 1 or self.feishu_chat_history_turns > 20:
            raise ValueError(
                "COMPANION_FEISHU_CHAT_HISTORY_TURNS must be between 1 and 20"
            )
        if self.feishu_chat_startup_timeout_seconds <= 0:
            raise ValueError(
                "COMPANION_FEISHU_CHAT_STARTUP_TIMEOUT_SECONDS must be positive"
            )
        if self.feishu_chat_enabled and (
            not all(feishu_values) or normalized_auth_token is None
        ):
            raise ValueError(
                "COMPANION_FEISHU_CHAT_ENABLED requires Feishu credentials, "
                "receiver open_id, and COMPANION_MINICPM_O_AUTH_TOKEN"
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
        if not isinstance(self.camera_enabled, bool):
            raise ValueError("COMPANION_CAMERA_ENABLED must be true or false")
        if not 1 <= self.camera_max_bytes <= 2_097_152:
            raise ValueError(
                "COMPANION_CAMERA_MAX_BYTES must be between 1 and 2097152"
            )
        if self.audio_queue_capacity < 1:
            raise ValueError("COMPANION_AUDIO_QUEUE_CAPACITY must be positive")
        if self.device_auto_stop_idle_seconds <= 0:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS must be positive"
            )
        if (
            self.device_auto_turn_rms_threshold is not None
            and self.device_auto_turn_rms_threshold < 0
        ):
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD must not be negative"
            )
        if (
            self.device_vad_turn_rms_threshold is not None
            and not math.isfinite(self.device_vad_turn_rms_threshold)
        ):
            raise ValueError(
                "COMPANION_DEVICE_VAD_TURN_RMS_THRESHOLD must be finite"
            )
        if (
            self.device_vad_turn_rms_threshold is not None
            and self.device_vad_turn_rms_threshold < 0
        ):
            raise ValueError(
                "COMPANION_DEVICE_VAD_TURN_RMS_THRESHOLD must not be negative"
            )
        if self.device_auto_turn_silence_frames < 1:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES must be positive"
            )
        if self.device_auto_turn_min_speech_frames < 1:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES must be positive"
            )
        if self.device_auto_turn_max_frames < 1:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_MAX_FRAMES must be positive"
            )
        if self.device_vad_post_tts_rms_threshold < 0:
            raise ValueError(
                "COMPANION_DEVICE_VAD_POST_TTS_RMS_THRESHOLD must not be negative"
            )
        if self.device_post_tts_silence_frames < 1:
            raise ValueError(
                "COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES must be positive"
            )
        if not isinstance(self.minicpm_o_model, str) or not self.minicpm_o_model.strip():
            raise ValueError("COMPANION_MINICPM_O_MODEL must not be empty")
        if self.task_scheduler_interval_seconds <= 0:
            raise ValueError(
                "COMPANION_TASK_SCHEDULER_INTERVAL_SECONDS must be positive"
            )
        if not isinstance(self.dynamic_agents_enabled, bool):
            raise ValueError(
                "COMPANION_DYNAMIC_AGENTS_ENABLED must be true or false"
            )
        if self.dynamic_agent_scheduler_interval_seconds <= 0:
            raise ValueError(
                "COMPANION_DYNAMIC_AGENT_SCHEDULER_INTERVAL_SECONDS "
                "must be positive"
            )
        if self.dynamic_agents_enabled:
            if normalized_dynamic_agent_owner_id is None:
                raise ValueError(
                    "COMPANION_DYNAMIC_AGENT_OWNER_ID is required when "
                    "dynamic Agents are enabled"
                )
            if normalized_dynamic_agent_target_device_id is None:
                raise ValueError(
                    "COMPANION_DYNAMIC_AGENT_TARGET_DEVICE_ID is required when "
                    "dynamic Agents are enabled"
                )
            if normalized_auth_token is None:
                raise ValueError(
                    "COMPANION_MINICPM_O_AUTH_TOKEN is required when dynamic Agents "
                    "are enabled"
                )
        if not 0 < self.device_conversation_idle_timeout_seconds <= 300:
            raise ValueError(
                "COMPANION_DEVICE_CONVERSATION_IDLE_TIMEOUT_SECONDS must be between 0 and 300"
            )
        if not isinstance(self.recent_context_enabled, bool):
            raise ValueError(
                "COMPANION_RECENT_CONTEXT_ENABLED must be true or false"
            )
        if self.recent_context_retention_days <= 0:
            raise ValueError(
                "COMPANION_RECENT_CONTEXT_RETENTION_DAYS must be positive"
            )
        if self.recent_context_max_messages <= 0:
            raise ValueError(
                "COMPANION_RECENT_CONTEXT_MAX_MESSAGES must be positive"
            )
        if self.recent_context_max_bytes < 256:
            raise ValueError(
                "COMPANION_RECENT_CONTEXT_MAX_BYTES must be at least 256"
            )
        normalized_subject_id = _normalize_optional_feishu_value(
            self.subject_id,
            name="COMPANION_SUBJECT_ID",
        )
        if normalized_subject_id is None:
            raise ValueError("COMPANION_SUBJECT_ID must not be empty")
        object.__setattr__(self, "ota_device_tokens", ota_tokens)
        object.__setattr__(self, "public_websocket_url", normalized_url)
        object.__setattr__(self, "device_token_hashes", normalized_hashes)
        object.__setattr__(self, "voice_runtime", normalized_runtime)
        object.__setattr__(self, "minicpm_o_endpoint", normalized_endpoint)
        object.__setattr__(self, "minicpm_o_auth_token", normalized_auth_token)
        object.__setattr__(
            self,
            "minicpm_o_compatible_base_url",
            normalized_minicpm_o_compatible_base_url,
        )
        object.__setattr__(self, "feishu_app_id", normalized_feishu_app_id)
        object.__setattr__(self, "feishu_app_secret", normalized_feishu_app_secret)
        object.__setattr__(
            self,
            "feishu_receiver_open_id",
            normalized_feishu_receiver_open_id,
        )
        object.__setattr__(
            self,
            "dynamic_agent_owner_id",
            normalized_dynamic_agent_owner_id,
        )
        object.__setattr__(
            self,
            "dynamic_agent_target_device_id",
            normalized_dynamic_agent_target_device_id,
        )
        object.__setattr__(self, "subject_id", normalized_subject_id)
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
        configured_minicpm_o_compatible_base_url = os.environ.get(
            "COMPANION_MINICPM_O_COMPATIBLE_BASE_URL",
            _DEFAULT_MINICPM_O_COMPATIBLE_BASE_URL,
        )
        configured_minicpm_o_model = os.environ.get(
            "COMPANION_MINICPM_O_MODEL",
            "MiniCPM-O-4.5-9B",
        )
        configured_minicpm_o_timeout = os.environ.get(
            "COMPANION_MINICPM_O_COMPATIBLE_TIMEOUT_SECONDS",
            "30",
        )
        configured_minicpm_o_compatible_max_retries = os.environ.get(
            "COMPANION_MINICPM_O_COMPATIBLE_MAX_RETRIES",
            "2",
        )
        configured_minicpm_o_retry_backoff = os.environ.get(
            "COMPANION_MINICPM_O_COMPATIBLE_RETRY_BACKOFF_SECONDS",
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
        feishu_chat_enabled = _parse_bool(
            os.environ.get("COMPANION_FEISHU_CHAT_ENABLED"),
            name="COMPANION_FEISHU_CHAT_ENABLED",
            default=False,
        )
        configured_feishu_chat_history_turns = os.environ.get(
            "COMPANION_FEISHU_CHAT_HISTORY_TURNS",
            "6",
        )
        configured_feishu_chat_startup_timeout = os.environ.get(
            "COMPANION_FEISHU_CHAT_STARTUP_TIMEOUT_SECONDS",
            "10",
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
        dynamic_agents_enabled = _parse_bool(
            os.environ.get("COMPANION_DYNAMIC_AGENTS_ENABLED"),
            name="COMPANION_DYNAMIC_AGENTS_ENABLED",
            default=False,
        )
        configured_dynamic_agent_owner_id = os.environ.get(
            "COMPANION_DYNAMIC_AGENT_OWNER_ID"
        )
        configured_dynamic_agent_target_device_id = os.environ.get(
            "COMPANION_DYNAMIC_AGENT_TARGET_DEVICE_ID"
        )
        configured_dynamic_agent_scheduler_interval = os.environ.get(
            "COMPANION_DYNAMIC_AGENT_SCHEDULER_INTERVAL_SECONDS",
            "1",
        )
        camera_enabled = _parse_bool(
            os.environ.get("COMPANION_CAMERA_ENABLED"),
            name="COMPANION_CAMERA_ENABLED",
            default=False,
        )
        configured_camera_max_bytes = os.environ.get(
            "COMPANION_CAMERA_MAX_BYTES",
            "2097152",
        )
        configured_conversation_idle_timeout = os.environ.get(
            "COMPANION_DEVICE_CONVERSATION_IDLE_TIMEOUT_SECONDS",
            "15",
        )
        device_continuous_conversation_enabled = _parse_bool(
            os.environ.get("COMPANION_DEVICE_CONTINUOUS_CONVERSATION_ENABLED"),
            name="COMPANION_DEVICE_CONTINUOUS_CONVERSATION_ENABLED",
            default=False,
        )
        recent_context_enabled = _parse_bool(
            os.environ.get("COMPANION_RECENT_CONTEXT_ENABLED"),
            name="COMPANION_RECENT_CONTEXT_ENABLED",
            default=False,
        )
        configured_recent_context_retention_days = os.environ.get(
            "COMPANION_RECENT_CONTEXT_RETENTION_DAYS",
            "7",
        )
        configured_recent_context_max_messages = os.environ.get(
            "COMPANION_RECENT_CONTEXT_MAX_MESSAGES",
            "20",
        )
        configured_recent_context_max_bytes = os.environ.get(
            "COMPANION_RECENT_CONTEXT_MAX_BYTES",
            "4096",
        )
        configured_subject_id = os.environ.get(
            "COMPANION_SUBJECT_ID",
            "voice-user",
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
        configured_device_auto_stop_idle_seconds = os.environ.get(
            "COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS",
            "1.2",
        )
        configured_device_auto_turn_rms_threshold = os.environ.get(
            "COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD"
        )
        configured_device_auto_turn_silence_frames = os.environ.get(
            "COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES",
            "12",
        )
        configured_device_vad_turn_rms_threshold = os.environ.get(
            "COMPANION_DEVICE_VAD_TURN_RMS_THRESHOLD"
        )
        configured_device_auto_turn_min_speech_frames = os.environ.get(
            "COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES",
            "5",
        )
        configured_device_auto_turn_max_frames = os.environ.get(
            "COMPANION_DEVICE_AUTO_TURN_MAX_FRAMES",
            "150",
        )
        configured_device_vad_post_tts_rms_threshold = os.environ.get(
            "COMPANION_DEVICE_VAD_POST_TTS_RMS_THRESHOLD",
            "35",
        )
        configured_device_post_tts_silence_frames = os.environ.get(
            "COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES",
            "3",
        )
        public_websocket_url = os.environ.get("COMPANION_PUBLIC_WEBSOCKET_URL")
        ota_tokens_json = os.environ.get("COMPANION_OTA_DEVICE_TOKENS", "{}")
        token_hashes_json = os.environ.get("COMPANION_DEVICE_TOKEN_HASHES", "{}")
        ota_tokens = json.loads(ota_tokens_json)
        token_hashes = json.loads(token_hashes_json)
        if dynamic_agents_enabled:
            configured_dynamic_agent_owner_id = (
                configured_dynamic_agent_owner_id
                or configured_feishu_receiver_open_id
            )
            if (
                not configured_dynamic_agent_target_device_id
                and isinstance(ota_tokens, dict)
                and len(ota_tokens) == 1
            ):
                configured_dynamic_agent_target_device_id = next(iter(ota_tokens))
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
            dynamic_agent_scheduler_interval_seconds = float(
                configured_dynamic_agent_scheduler_interval
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DYNAMIC_AGENT_SCHEDULER_INTERVAL_SECONDS "
                "must be a number"
            ) from exc
        try:
            device_conversation_idle_timeout_seconds = float(
                configured_conversation_idle_timeout
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_CONVERSATION_IDLE_TIMEOUT_SECONDS must be a number"
            ) from exc
        try:
            recent_context_retention_days = int(
                configured_recent_context_retention_days
            )
            recent_context_max_messages = int(
                configured_recent_context_max_messages
            )
            recent_context_max_bytes = int(configured_recent_context_max_bytes)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_RECENT_CONTEXT limits must be integers"
            ) from exc
        try:
            camera_max_bytes = int(configured_camera_max_bytes)
        except ValueError as exc:
            raise ValueError("COMPANION_CAMERA_MAX_BYTES must be an integer") from exc
        try:
            minicpm_o_compatible_timeout_seconds = float(configured_minicpm_o_timeout)
        except ValueError as exc:
            raise ValueError("COMPANION_MINICPM_O_COMPATIBLE_TIMEOUT_SECONDS must be a number") from exc
        try:
            minicpm_o_compatible_max_retries = int(configured_minicpm_o_compatible_max_retries)
        except ValueError as exc:
            raise ValueError("COMPANION_MINICPM_O_COMPATIBLE_MAX_RETRIES must be an integer") from exc
        try:
            minicpm_o_compatible_retry_backoff_seconds = float(configured_minicpm_o_retry_backoff)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_MINICPM_O_COMPATIBLE_RETRY_BACKOFF_SECONDS must be a number"
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
            feishu_chat_history_turns = int(configured_feishu_chat_history_turns)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_FEISHU_CHAT_HISTORY_TURNS must be an integer"
            ) from exc
        try:
            feishu_chat_startup_timeout_seconds = float(
                configured_feishu_chat_startup_timeout
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_FEISHU_CHAT_STARTUP_TIMEOUT_SECONDS must be a number"
            ) from exc
        try:
            audio_queue_capacity = int(configured_audio_queue_capacity)
        except ValueError as exc:
            raise ValueError(
                "COMPANION_AUDIO_QUEUE_CAPACITY must be an integer"
            ) from exc
        try:
            device_auto_stop_idle_seconds = float(
                configured_device_auto_stop_idle_seconds
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_STOP_IDLE_SECONDS must be a number"
            ) from exc
        try:
            device_auto_turn_rms_threshold = (
                float(configured_device_auto_turn_rms_threshold)
                if configured_device_auto_turn_rms_threshold is not None
                else None
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_RMS_THRESHOLD must be a number"
            ) from exc
        try:
            device_vad_turn_rms_threshold = (
                float(configured_device_vad_turn_rms_threshold.strip())
                if configured_device_vad_turn_rms_threshold is not None
                and configured_device_vad_turn_rms_threshold.strip()
                else None
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_VAD_TURN_RMS_THRESHOLD must be a number"
            ) from exc
        try:
            device_auto_turn_silence_frames = int(
                configured_device_auto_turn_silence_frames
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_SILENCE_FRAMES must be an integer"
            ) from exc
        try:
            device_auto_turn_min_speech_frames = int(
                configured_device_auto_turn_min_speech_frames
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_MIN_SPEECH_FRAMES must be an integer"
            ) from exc
        try:
            device_auto_turn_max_frames = int(
                configured_device_auto_turn_max_frames
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_AUTO_TURN_MAX_FRAMES must be an integer"
            ) from exc
        try:
            device_vad_post_tts_rms_threshold = float(
                configured_device_vad_post_tts_rms_threshold
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_VAD_POST_TTS_RMS_THRESHOLD must be a number"
            ) from exc
        try:
            device_post_tts_silence_frames = int(
                configured_device_post_tts_silence_frames
            )
        except ValueError as exc:
            raise ValueError(
                "COMPANION_DEVICE_POST_TTS_SILENCE_FRAMES must be an integer"
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
            minicpm_o_compatible_base_url=configured_minicpm_o_compatible_base_url,
            minicpm_o_model=configured_minicpm_o_model,
            minicpm_o_compatible_timeout_seconds=minicpm_o_compatible_timeout_seconds,
            minicpm_o_compatible_max_retries=minicpm_o_compatible_max_retries,
            minicpm_o_compatible_retry_backoff_seconds=minicpm_o_compatible_retry_backoff_seconds,
            feishu_app_id=configured_feishu_app_id,
            feishu_app_secret=configured_feishu_app_secret,
            feishu_receiver_open_id=configured_feishu_receiver_open_id,
            feishu_base_url=configured_feishu_base_url,
            feishu_timeout_seconds=feishu_timeout_seconds,
            feishu_max_retries=feishu_max_retries,
            feishu_retry_backoff_seconds=feishu_retry_backoff_seconds,
            feishu_chat_enabled=feishu_chat_enabled,
            feishu_chat_history_turns=feishu_chat_history_turns,
            feishu_chat_startup_timeout_seconds=(
                feishu_chat_startup_timeout_seconds
            ),
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
            dynamic_agents_enabled=dynamic_agents_enabled,
            dynamic_agent_owner_id=configured_dynamic_agent_owner_id,
            dynamic_agent_target_device_id=(
                configured_dynamic_agent_target_device_id
            ),
            dynamic_agent_scheduler_interval_seconds=(
                dynamic_agent_scheduler_interval_seconds
            ),
            device_conversation_idle_timeout_seconds=(
                device_conversation_idle_timeout_seconds
            ),
            device_continuous_conversation_enabled=(
                device_continuous_conversation_enabled
            ),
            recent_context_enabled=recent_context_enabled,
            recent_context_retention_days=recent_context_retention_days,
            recent_context_max_messages=recent_context_max_messages,
            recent_context_max_bytes=recent_context_max_bytes,
            subject_id=configured_subject_id,
            camera_enabled=camera_enabled,
            camera_max_bytes=camera_max_bytes,
            device_auto_stop_idle_seconds=device_auto_stop_idle_seconds,
            device_vad_turn_rms_threshold=device_vad_turn_rms_threshold,
            device_auto_turn_rms_threshold=device_auto_turn_rms_threshold,
            device_auto_turn_silence_frames=device_auto_turn_silence_frames,
            device_auto_turn_min_speech_frames=device_auto_turn_min_speech_frames,
            device_auto_turn_max_frames=device_auto_turn_max_frames,
            device_vad_post_tts_rms_threshold=(
                device_vad_post_tts_rms_threshold
            ),
            device_post_tts_silence_frames=device_post_tts_silence_frames,
            audio_queue_capacity=audio_queue_capacity,
        )
