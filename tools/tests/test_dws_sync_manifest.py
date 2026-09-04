from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from companion_gateway.project.sync_models import SyncSourceType
from tools.dws_sync.manifest import DwsManifest, DwsSourceSpec


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
