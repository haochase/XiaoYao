from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from companion_gateway.project.sync_models import SyncSourceType
from tools.dws_sync.manifest import (
    _MAX_MANIFEST_BYTES,
    DwsManifest,
    DwsSourceSpec,
)


class FakeManifestStream:
    def __init__(self, raw: bytes, events: list[object]) -> None:
        self._raw = raw
        self._events = events

    def __enter__(self) -> "FakeManifestStream":
        self._events.append("enter")
        return self

    def __exit__(self, *_args: object) -> None:
        self._events.append("exit")

    def fileno(self) -> int:
        self._events.append("fileno")
        return 73

    def read(self, size: int = -1) -> bytes:
        self._events.append(("read", size))
        return self._raw


class TrackingOpenedStat:
    def __init__(self, size: int, events: list[object]) -> None:
        self._size = size
        self._events = events

    @property
    def st_mode(self) -> int:
        self._events.append("st_mode")
        return stat.S_IFREG

    @property
    def st_size(self) -> int:
        self._events.append("st_size")
        return self._size


def write_manifest(
    tmp_path: Path,
    *,
    sources: list[dict[str, object]] | None = None,
    project_extra: dict[str, object] | None = None,
    root_extra: dict[str, object] | None = None,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "projects": [
            {
                "project_id": "project-1",
                "project_name": "Demo",
                "profile": "corp:user",
                "permission_scope": "project:project-1",
                "sources": sources
                or [{"source_type": "document", "source_id": "doc-001"}],
                **(project_extra or {}),
            }
        ],
        **(root_extra or {}),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_manifest_uses_one_opened_descriptor_and_one_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_manifest(tmp_path)
    raw = path.read_bytes()
    events: list[object] = []
    real_exists = Path.exists
    real_is_file = Path.is_file
    real_open = Path.open
    real_read_bytes = Path.read_bytes
    real_stat = Path.stat

    def guard_path_query(real_query: Any) -> Any:
        def guarded_query(
            queried_path: Path,
            *args: object,
            **kwargs: object,
        ) -> Any:
            if queried_path == path:
                raise AssertionError("path query introduces a TOCTOU window")
            return real_query(queried_path, *args, **kwargs)

        return guarded_query

    def guarded_stat(
        queried_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if queried_path == path and follow_symlinks:
            raise AssertionError("path query introduces a TOCTOU window")
        return real_stat(queried_path, follow_symlinks=follow_symlinks)

    def open_once(
        opened_path: Path,
        mode: str = "r",
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        if opened_path != path:
            return real_open(opened_path, mode, *_args, **_kwargs)
        events.append(("open", opened_path, mode))
        if events.count(("open", opened_path, mode)) > 1:
            raise AssertionError("manifest opened more than once")
        return FakeManifestStream(raw, events)

    def opened_fstat(file_descriptor: int) -> TrackingOpenedStat:
        events.append(("fstat", file_descriptor))
        return TrackingOpenedStat(len(raw), events)

    monkeypatch.setattr(Path, "exists", guard_path_query(real_exists))
    monkeypatch.setattr(Path, "is_file", guard_path_query(real_is_file))
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "read_bytes", guard_path_query(real_read_bytes))
    monkeypatch.setattr(Path, "open", open_once)
    monkeypatch.setattr(os, "fstat", opened_fstat)

    manifest = DwsManifest.load(path)

    assert manifest.projects[0].project_id == "project-1"
    assert events == [
        ("open", path, "rb"),
        "enter",
        "fileno",
        ("fstat", 73),
        "st_mode",
        "st_size",
        ("read", _MAX_MANIFEST_BYTES + 1),
        "exit",
    ]


def test_manifest_rejects_actual_bounded_read_larger_than_fstat_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_manifest(tmp_path)
    events: list[object] = []
    stream = FakeManifestStream(b" " * (_MAX_MANIFEST_BYTES + 1), events)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFREG, st_size=1),
    )

    with pytest.raises(ValueError) as error:
        DwsManifest.load(path)

    assert str(error.value) == "manifest_too_large"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert events.count(("read", _MAX_MANIFEST_BYTES + 1)) == 1


def test_manifest_rejects_non_regular_opened_descriptor_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_manifest(tmp_path)
    events: list[object] = []
    stream = FakeManifestStream(b"must-not-be-read", events)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR, st_size=0),
    )

    with pytest.raises(ValueError) as error:
        DwsManifest.load(path)

    assert str(error.value) == "manifest_not_regular_file"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not any(
        isinstance(event, tuple) and event[0] == "read" for event in events
    )


def test_manifest_treats_opened_descriptor_as_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_manifest(tmp_path)
    raw = path.read_bytes()
    events: list[object] = []
    stream = FakeManifestStream(raw, events)

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFLNK),
    )
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_size=len(raw),
        ),
    )

    manifest = DwsManifest.load(path)

    assert manifest.projects[0].project_id == "project-1"
    assert events.count(("read", _MAX_MANIFEST_BYTES + 1)) == 1


def test_manifest_path_errors_keep_stable_labels_without_exception_chains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for path, expected in (
        (tmp_path / "missing.json", "manifest_not_found"),
        (tmp_path, "manifest_not_regular_file"),
    ):
        with pytest.raises(ValueError) as error:
            DwsManifest.load(path)
        assert str(error.value) == expected
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    denied = write_manifest(tmp_path)

    def deny_open(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", deny_open)

    with pytest.raises(ValueError) as error:
        DwsManifest.load(denied)

    assert str(error.value) == "manifest_unreadable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_manifest_loads_only_declared_project_source_fields(tmp_path: Path) -> None:
    path = write_manifest(
        tmp_path,
        sources=[
            {"source_type": "document", "source_id": "doc-001"},
            {
                "source_type": "calendar",
                "source_id": "event-001",
                "window_start": "2026-09-01T00:00:00+08:00",
                "window_end": "2026-09-02T00:00:00+08:00",
            },
        ],
    )

    manifest = DwsManifest.load(path)

    assert manifest.schema_version == 1
    assert manifest.projects[0].project_id == "project-1"
    assert manifest.projects[0].sources[0].source_type is SyncSourceType.DOCUMENT
    assert manifest.projects[0].sources[1].window_start is not None
    with pytest.raises(ValidationError):
        manifest.projects[0].profile = "other"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["command", "argv", "token", "gateway"])
def test_manifest_rejects_command_and_secret_fields(
    tmp_path: Path,
    field: str,
) -> None:
    path = write_manifest(tmp_path, root_extra={field: "private"})

    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(path)


def test_manifest_rejects_more_than_thirty_and_duplicate_sources(
    tmp_path: Path,
) -> None:
    too_many = write_manifest(
        tmp_path,
        sources=[
            {"source_type": "document", "source_id": f"doc-{index}"}
            for index in range(31)
        ],
    )
    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(too_many)

    duplicate = write_manifest(
        tmp_path,
        sources=[
            {"source_type": "task", "source_id": "task-1"},
            {"source_type": "task", "source_id": "task-1"},
        ],
    )
    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(duplicate)


def test_calendar_window_is_required_bounded_and_calendar_only(
    tmp_path: Path,
) -> None:
    missing_end = write_manifest(
        tmp_path,
        sources=[
            {
                "source_type": "calendar",
                "source_id": "event-1",
                "window_start": "2026-09-01T00:00:00+08:00",
            }
        ],
    )
    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(missing_end)

    too_wide = write_manifest(
        tmp_path,
        sources=[
            {
                "source_type": "calendar",
                "source_id": "event-1",
                "window_start": "2025-01-01T00:00:00+08:00",
                "window_end": "2026-09-01T00:00:00+08:00",
            }
        ],
    )
    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(too_wide)

    with pytest.raises(ValidationError):
        DwsSourceSpec(
            source_type="task",
            source_id="task-1",
            window_start="2026-09-01T00:00:00+08:00",
            window_end="2026-09-02T00:00:00+08:00",
        )


def test_manifest_requires_absolute_regular_small_utf8_json_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="manifest_path_not_absolute"):
        DwsManifest.load(Path("manifest.json"))

    with pytest.raises(ValueError, match="manifest_not_found"):
        DwsManifest.load(tmp_path / "missing.json")

    with pytest.raises(ValueError, match="manifest_not_regular_file"):
        DwsManifest.load(tmp_path)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xffprivate-secret")
    with pytest.raises(ValueError) as error:
        DwsManifest.load(invalid_utf8)
    assert str(error.value) == "manifest_invalid_utf8"
    assert "private-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text('{"private":"secret"', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        DwsManifest.load(invalid_json)
    assert str(error.value) == "manifest_invalid_json"
    assert "private" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="manifest_too_large"):
        DwsManifest.load(oversized)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_manifest_rejects_non_finite_json_without_exception_chain(
    tmp_path: Path,
    constant: str,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"schema_version":1,"projects":' + constant + "}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        DwsManifest.load(path)

    assert str(error.value) == "manifest_invalid_json"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_manifest_validation_error_has_no_sensitive_exception_chain(
    tmp_path: Path,
) -> None:
    path = write_manifest(tmp_path, root_extra={"token": "private-token"})

    with pytest.raises(ValueError) as error:
        DwsManifest.load(path)

    assert str(error.value) == "manifest_validation_failed"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_manifest_rejects_duplicate_projects_and_control_characters(
    tmp_path: Path,
) -> None:
    base = {
        "project_id": "project-1",
        "project_name": "Demo",
        "profile": "corp:user",
        "permission_scope": "project:project-1",
        "sources": [{"source_type": "document", "source_id": "doc-001"}],
    }
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "projects": [base, base]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest_validation_failed"):
        DwsManifest.load(path)

    with pytest.raises(ValidationError):
        DwsSourceSpec(source_type="document", source_id="doc\nsecret")
