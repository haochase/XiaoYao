from __future__ import annotations

import json
import os
import re
import stat
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


def _reject_non_finite_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


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

        path_error: str | None = None
        path_mode: int | None = None
        try:
            path_mode = path.lstat().st_mode
        except FileNotFoundError:
            path_error = "manifest_not_found"
        except OSError:
            path_error = "manifest_unreadable"
        if path_error is not None:
            raise ValueError(path_error)
        assert path_mode is not None
        if stat.S_ISFIFO(path_mode):
            raise ValueError("manifest_not_regular_file")

        read_error: str | None = None
        descriptor_error: str | None = None
        raw: bytes | None = None
        try:
            with path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened_stat.st_mode):
                    descriptor_error = "manifest_not_regular_file"
                elif opened_stat.st_size > _MAX_MANIFEST_BYTES:
                    descriptor_error = "manifest_too_large"
                else:
                    raw = stream.read(_MAX_MANIFEST_BYTES + 1)
        except FileNotFoundError:
            read_error = "manifest_not_found"
        except IsADirectoryError:
            read_error = "manifest_not_regular_file"
        except PermissionError:
            read_error = (
                "manifest_not_regular_file"
                if stat.S_ISDIR(path_mode)
                else "manifest_unreadable"
            )
        except OSError:
            read_error = "manifest_unreadable"
        if read_error is not None:
            raise ValueError(read_error)
        if descriptor_error is not None:
            raise ValueError(descriptor_error)
        assert raw is not None
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest_too_large")

        decode_failed = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            decode_failed = True
        if decode_failed:
            raise ValueError("manifest_invalid_utf8")

        json_failed = False
        try:
            payload = json.loads(
                text,
                parse_constant=_reject_non_finite_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError):
            json_failed = True
        if json_failed:
            raise ValueError("manifest_invalid_json")
        if not isinstance(payload, dict):
            raise ValueError("manifest_root_invalid")

        validation_failed = False
        try:
            manifest = cls.model_validate(payload)
        except (TypeError, ValueError):
            validation_failed = True
        if validation_failed:
            raise ValueError("manifest_validation_failed")
        return manifest
