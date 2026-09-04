from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.project.sync_models import SyncSourceType


_MAX_MANIFEST_BYTES = 1024 * 1024
_PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def _require_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class DwsSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SyncSourceType
    source_id: str = Field(min_length=1, max_length=256)
    window_start: datetime | None = None
    window_end: datetime | None = None

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        _require_non_blank(value, "source_id")
        if _CONTROL_CHARACTER_PATTERN.search(value) is not None:
            raise ValueError("source_id contains control characters")
        return value

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_window_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            _require_aware(value, "calendar window")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "DwsSourceSpec":
        has_start = self.window_start is not None
        has_end = self.window_end is not None
        if self.source_type is SyncSourceType.CALENDAR:
            if not has_start or not has_end:
                raise ValueError("calendar source requires a complete window")
            assert self.window_start is not None
            assert self.window_end is not None
            if self.window_end <= self.window_start:
                raise ValueError("calendar window end must be after start")
            if self.window_end - self.window_start > timedelta(days=366):
                raise ValueError("calendar window must be at most 366 days")
        elif has_start or has_end:
            raise ValueError("only calendar sources accept a window")
        return self


class DwsProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=512)
    profile: str = Field(min_length=1, max_length=256)
    permission_scope: str = Field(min_length=1, max_length=256)
    sources: tuple[DwsSourceSpec, ...] = Field(min_length=1, max_length=30)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        if _PROJECT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("project_id is invalid")
        return value

    @field_validator("project_name", "profile", "permission_scope")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        return _require_non_blank(value, "manifest field")

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "DwsProjectManifest":
        identities = {(item.source_type, item.source_id) for item in self.sources}
        if len(identities) != len(self.sources):
            raise ValueError("source identities must be unique")
        return self


class DwsManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    projects: tuple[DwsProjectManifest, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_projects(self) -> "DwsManifest":
        project_ids = {item.project_id for item in self.projects}
        if len(project_ids) != len(self.projects):
            raise ValueError("project IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> "DwsManifest":
        if not path.is_absolute():
            raise ValueError("manifest_path_not_absolute")
        if not path.exists():
            raise ValueError("manifest_not_found")
        if not path.is_file():
            raise ValueError("manifest_not_regular_file")
        try:
            if path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise ValueError("manifest_too_large")
            raw = path.read_bytes()
        except ValueError:
            raise
        except OSError as error:
            raise ValueError("manifest_unreadable") from error
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("manifest_invalid_utf8") from error
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError("manifest_invalid_json") from error
        if not isinstance(payload, dict):
            raise ValueError("manifest_root_invalid")
        try:
            return cls.model_validate(payload)
        except (TypeError, ValueError) as error:
            raise ValueError("manifest_validation_failed") from error
