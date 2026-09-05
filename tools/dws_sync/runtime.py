from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.project.protection import ContentProtector
from tools.dws_sync.manifest import DwsManifest, DwsProjectManifest
from tools.dws_sync.launch import resolve_dws_launch


CONFIG_NAME = ".private/qwenwork-dws-project-sync.json"
RUNTIME_NAME = ".private/dws-runtime"


def runtime_database(root: Path) -> Path:
    path = root / RUNTIME_NAME / "companion.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        require_local(candidate)
        if candidate.exists():
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("runtime_database_invalid")
    return path


def require_local(path: Path, *, drives: tuple[str, ...] = ("E:",)) -> None:
    if not path.is_absolute() or path.drive.upper() not in drives:
        raise ValueError("runtime_path_invalid")
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("runtime_path_redirected")


def read_object(path: Path) -> dict[str, object]:
    require_local(path)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("runtime_file_invalid")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("runtime_file_invalid")
    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("runtime_file_invalid")
    return result


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    manifest: Path
    project: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    dws: Path
    source_bundle: Path
    context_artifact: Path
    state: Path

    @field_validator("manifest", "source_bundle", "context_artifact", "state")
    @classmethod
    def private_path(cls, value: Path) -> Path:
        require_local(value)
        return value

    @field_validator("dws")
    @classmethod
    def input_path(cls, value: Path) -> Path:
        require_local(value, drives=("C:", "E:"))
        return value

    @model_validator(mode="after")
    def distinct(self) -> "TaskConfig":
        paths = (
            self.manifest, self.dws, self.source_bundle, self.context_artifact, self.state
        )
        normalized = [os.path.normcase(str(path.resolve())) for path in paths]
        if len(set(normalized)) != len(paths):
            raise ValueError("runtime_paths_overlap")
        for index, path in enumerate(paths):
            for other in paths[index + 1:]:
                if path.exists() and other.exists() and path.samefile(other):
                    raise ValueError("runtime_paths_overlap")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _binding(config: TaskConfig, project: DwsProjectManifest) -> str:
    payload = {
        "config": config.model_dump(mode="json"),
        "project": project.model_dump(mode="json"),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _project(manifest: Path, project_id: str) -> DwsProjectManifest:
    result = next(
        (item for item in DwsManifest.load(manifest).projects if item.project_id == project_id),
        None,
    )
    if result is None:
        raise ValueError("runtime_project_missing")
    return result


def prepare_runtime(
    root: Path, manifest: Path, project_id: str, dws: Path, protector: ContentProtector
) -> None:
    require_local(root)
    require_local(manifest)
    project = _project(manifest, project_id)
    require_local(dws, drives=("C:", "E:"))
    if not dws.is_file():
        raise ValueError("runtime_dws_missing")
    resolve_dws_launch(dws)
    runtime = root / RUNTIME_NAME
    config_path = root / CONFIG_NAME
    require_local(runtime)
    require_local(config_path)
    if runtime.exists() or config_path.exists():
        raise FileExistsError("runtime_exists")
    config = TaskConfig(
        schema_version=1,
        manifest=manifest, project=project_id, dws=dws,
        source_bundle=runtime / "source-bundle.json",
        context_artifact=runtime / "context-artifact.json",
        state=runtime / "sync-state.json",
    )
    token = secrets.token_urlsafe(32)
    secret = {"binding": _binding(config, project), "token": token}
    protected = protector.protect(project_id, _canonical(secret))
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.mkdir()
    _write_new(runtime / "credential.dpapi", protected)
    _write_new(config_path, _canonical(config.model_dump(mode="json")))


def load_runtime(
    root: Path, protector: ContentProtector
) -> tuple[TaskConfig, DwsProjectManifest, str]:
    require_local(root)
    config_path = root / CONFIG_NAME
    config = TaskConfig.model_validate(read_object(config_path))
    paths = (
        config.manifest, config.dws, config.source_bundle, config.context_artifact, config.state
    )
    for path in paths:
        if path.resolve() == config_path.resolve() or (
            path.exists() and path.samefile(config_path)
        ):
            raise ValueError("runtime_paths_overlap")
        if not path.parent.is_dir():
            raise ValueError("runtime_parent_missing")
    project = _project(config.manifest, config.project)
    credential_path = root / RUNTIME_NAME / "credential.dpapi"
    require_local(credential_path)
    failed = False
    try:
        with credential_path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError("credential_file_invalid")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("credential_file_invalid")
        secret = json.loads(protector.unprotect(config.project, raw))
        if set(secret) != {"binding", "token"}:
            raise ValueError("credential_invalid")
        token = secret["token"]
        if not isinstance(token, str) or not 32 <= len(token) <= 128 or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in token
        ):
            raise ValueError("credential_invalid")
    except Exception:
        failed = True
    if failed:
        raise ValueError("runtime_credential_invalid")
    if not isinstance(secret["binding"], str) or not hmac.compare_digest(
        secret["binding"], _binding(config, project)
    ):
        raise ValueError("runtime_binding_invalid")
    return config, project, token
