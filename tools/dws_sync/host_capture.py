from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import cast

from companion_gateway.project.protection import ContentProtector
from companion_gateway.project.sync_models import SyncSourceType
from tools.dws_sync import state_lock
from tools.dws_sync.adapters import document_metadata_contract, unwrap_dws_payload
from tools.dws_sync.host_bridge import (
    MAX_RESULT_BYTES,
    _decode_json,
    decode_host_result,
)
from tools.dws_sync.manifest import DwsProjectManifest
from tools.dws_sync.runner import DwsReadError


MAX_CAPTURE_BYTES = MAX_RESULT_BYTES + 65_536
_PROJECT_PRIVATE_ROOT = Path(__file__).resolve().parents[2] / ".private"
_DEFAULT_CAPTURE_ROOT = _PROJECT_PRIVATE_ROOT / "dws-host-captures"
PRIVATE_CAPTURE_ROOT = _DEFAULT_CAPTURE_ROOT
_TEST_CAPTURE_ROOT: Path | None = None
_CAPTURE_SCHEMA_VERSION = 1
_CAPTURE_KEYS = {
    "schema_version",
    "project_key",
    "source_key",
    "run_token_key",
    "document_info",
}
_BINDING_KEY = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = 0x400
_MISSING = object()
_TITLE_FIELDS = ("source_title", "title", "name", "summary", "subject")
_URL_FIELDS = ("source_url", "url", "link", "shareUrl")
_VERSION_FIELDS = (
    "source_version",
    "version",
    "revision",
    "updatedAt",
    "updateTime",
)
_TIME_FIELDS = (
    "source_time",
    "updatedAt",
    "updateTime",
    "startTime",
    "createdAt",
    "createTime",
)


@dataclass(frozen=True)
class DocumentInfoCapture:
    document_info: Mapping[str, object]


class DocumentInfoCaptureWrite:
    def __init__(
        self,
        project: DwsProjectManifest,
        source_id: str,
        run_token: str,
        document_info: dict[str, object],
        protector: ContentProtector,
        root: Path,
    ) -> None:
        self._project = project
        self._source_id = source_id
        self._run_token = run_token
        self._document_info = document_info
        self._protector = protector
        self._root = root
        self._path = document_info_capture_path(project.project_id)
        self._capture = _document_capture(document_info)
        self._protected: bytes | None = None
        self._staged_info: os.stat_result | None = None
        self._publish_attempted = False
        self._applied = False
        self._rolled_back = False

    @property
    def capture(self) -> DocumentInfoCapture:
        return self._capture

    def apply(self) -> None:
        if self._applied:
            raise ValueError("host_capture_invalid")
        _ensure_capture_root(self._root)
        with state_lock.acquire_state_lock(
            self._path,
            self._project.project_id,
            root=self._root,
        ):
            _ensure_capture_root(self._root)
            existing = _read_capture(self._path)
            if existing is None:
                try:
                    protected = self._protector.protect(
                        self._project.project_id,
                        _capture_payload(
                            self._project.project_id,
                            self._source_id,
                            self._run_token,
                            self._document_info,
                        ),
                    )
                except Exception:
                    raise ValueError("host_capture_invalid") from None
                self._protected = protected
                _write_capture(
                    self._path,
                    protected,
                    on_publish=self._stage_publish,
                )
            else:
                captured = _load_protected_capture(
                    existing,
                    self._project,
                    self._source_id,
                    self._run_token,
                    self._protector,
                )
                if _canonical_json(_thaw(captured.document_info)) != _canonical_json(
                    self._document_info
                ):
                    raise ValueError("host_capture_conflict")
            self._applied = True

    def _stage_publish(self, staged_info: os.stat_result) -> None:
        self._staged_info = staged_info
        self._publish_attempted = True

    def rollback(self) -> None:
        if self._rolled_back or not self._publish_attempted:
            return
        protected = self._protected
        staged_info = self._staged_info
        if protected is None or staged_info is None:
            raise ValueError("host_capture_invalid")
        with state_lock.acquire_state_lock(
            self._path,
            self._project.project_id,
            root=self._root,
        ):
            _ensure_capture_root(self._root)
            snapshot = _read_capture_snapshot(self._path)
            if snapshot is not None:
                current, current_info = snapshot
                if not hmac.compare_digest(
                    current, protected
                ) or not os.path.samestat(current_info, staged_info):
                    raise ValueError("host_capture_invalid")
                _delete_capture(
                    self._path,
                    expected=protected,
                    expected_info=staged_info,
                )
        self._rolled_back = True


class DocumentInfoCaptureDelete:
    def __init__(
        self,
        project: DwsProjectManifest,
        source_id: str,
        run_token: str,
        protector: ContentProtector,
        root: Path,
    ) -> None:
        self._project = project
        self._source_id = source_id
        self._run_token = run_token
        self._protector = protector
        self._root = root
        self._path = document_info_capture_path(project.project_id)
        self._protected: bytes | None = None
        self._path_info: os.stat_result | None = None
        self._capture: DocumentInfoCapture | None = None
        self._delete_attempted = False
        self._deleted = False
        self._rolled_back = False

    def load(self) -> DocumentInfoCapture:
        if self._capture is not None:
            return self._capture
        _ensure_capture_root(self._root)
        with state_lock.acquire_state_lock(
            self._path,
            self._project.project_id,
            root=self._root,
        ):
            _ensure_capture_root(self._root)
            snapshot = _read_capture_snapshot(self._path)
            if snapshot is None:
                raise ValueError("host_capture_missing")
            protected, path_info = snapshot
            self._capture = _load_protected_capture(
                protected,
                self._project,
                self._source_id,
                self._run_token,
                self._protector,
            )
            self._protected = protected
            self._path_info = path_info
            return self._capture

    def apply(self) -> None:
        if (
            self._deleted
            or self._protected is None
            or self._path_info is None
        ):
            raise ValueError("host_capture_invalid")
        _ensure_capture_root(self._root)
        with state_lock.acquire_state_lock(
            self._path,
            self._project.project_id,
            root=self._root,
        ):
            _ensure_capture_root(self._root)
            current = _read_capture(self._path)
            if current is None or not hmac.compare_digest(
                current, self._protected
            ):
                raise ValueError("host_capture_invalid")
            self._delete_attempted = True
            _delete_capture(
                self._path,
                expected=self._protected,
                expected_info=self._path_info,
            )
        self._deleted = True

    def rollback(self) -> None:
        if self._rolled_back or not self._delete_attempted:
            return
        protected = self._protected
        path_info = self._path_info
        if protected is None or path_info is None:
            raise ValueError("host_capture_invalid")
        with state_lock.acquire_state_lock(
            self._path,
            self._project.project_id,
            root=self._root,
        ):
            _ensure_capture_root(self._root)
            current = _read_capture_snapshot(self._path)
            if current is None:
                _write_capture(self._path, protected)
            else:
                current_bytes, current_info = current
                if not hmac.compare_digest(
                    current_bytes, protected
                ) or not os.path.samestat(current_info, path_info):
                    raise ValueError("host_capture_invalid")
        self._rolled_back = True


def _binding_key(label: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("host_capture_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("host_capture_invalid") from None
    return hashlib.sha256(
        b"hui-anchor-host-capture-v1\0" + label.encode("ascii") + b"\0" + encoded
    ).hexdigest()


def _validate_capture_root_path(root: Path) -> None:
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or root.drive.upper() != "E:"
    ):
        raise ValueError("host_capture_invalid")
    text = str(root)
    if text != os.path.normpath(text):
        raise ValueError("host_capture_invalid")
    for component in root.parts[1:]:
        if (
            not component
            or component in {".", ".."}
            or component.rstrip(" .") != component
        ):
            raise ValueError("host_capture_invalid")


def _capture_root(root: Path | None = None) -> Path:
    if root is not None:
        raise ValueError("host_capture_invalid")
    if _TEST_CAPTURE_ROOT is not None:
        selected = _TEST_CAPTURE_ROOT
    else:
        if PRIVATE_CAPTURE_ROOT != _DEFAULT_CAPTURE_ROOT:
            raise ValueError("host_capture_invalid")
        selected = _DEFAULT_CAPTURE_ROOT
    _validate_capture_root_path(selected)
    return selected


def document_info_capture_path(
    project_id: str,
    *,
    root: Path | None = None,
) -> Path:
    return _capture_root(root) / f"{_binding_key('project', project_id)}.dpapi"


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _safe_capture_stat(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not _is_reparse(info)
        and info.st_nlink == 1
        and 1 <= info.st_size <= MAX_CAPTURE_BYTES
    )


def _ensure_capture_root(root: Path) -> None:
    _validate_capture_root_path(root)
    for current in (*reversed(root.parents), root):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("host_capture_invalid") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            raise ValueError("host_capture_invalid")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError("host_capture_invalid") from None
    try:
        info = root.lstat()
    except OSError:
        raise ValueError("host_capture_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise ValueError("host_capture_invalid")


def _read_capture_snapshot(
    path: Path,
) -> tuple[bytes, os.stat_result] | None:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("host_capture_invalid") from None
    if not _safe_capture_stat(path_info):
        raise ValueError("host_capture_invalid")
    try:
        with path.open("rb") as stream:
            opened_info = os.fstat(stream.fileno())
            if (
                not _safe_capture_stat(opened_info)
                or not os.path.samestat(path_info, opened_info)
            ):
                raise ValueError("host_capture_invalid")
            raw = stream.read(MAX_CAPTURE_BYTES + 1)
            final_opened_info = os.fstat(stream.fileno())
        final_path_info = path.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("host_capture_invalid") from None
    if (
        len(raw) > MAX_CAPTURE_BYTES
        or not _safe_capture_stat(final_opened_info)
        or not _safe_capture_stat(final_path_info)
        or not os.path.samestat(path_info, final_opened_info)
        or not os.path.samestat(path_info, final_path_info)
    ):
        raise ValueError("host_capture_invalid")
    return raw, final_path_info


def _read_capture(path: Path) -> bytes | None:
    snapshot = _read_capture_snapshot(path)
    return None if snapshot is None else snapshot[0]


def _delete_capture(
    path: Path,
    *,
    expected: bytes | None = None,
    expected_info: os.stat_result | None = None,
) -> bool:
    current = _read_capture(path)
    if current is None:
        return False
    if expected is not None and not hmac.compare_digest(current, expected):
        raise ValueError("host_capture_invalid")
    try:
        current_info = path.lstat()
        if not _safe_capture_stat(current_info) or (
            expected_info is not None
            and not os.path.samestat(expected_info, current_info)
        ):
            raise ValueError("host_capture_invalid")
        path.unlink()
    except ValueError:
        raise
    except OSError:
        raise ValueError("host_capture_invalid") from None
    return True


def _write_capture(
    path: Path,
    protected: bytes,
    *,
    on_publish: Callable[[os.stat_result], None] | None = None,
) -> None:
    if type(protected) is not bytes or not 1 <= len(protected) <= MAX_CAPTURE_BYTES:
        raise ValueError("host_capture_invalid")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise ValueError("host_capture_invalid") from None
    else:
        raise ValueError("host_capture_invalid")

    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(protected)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_info = temporary.lstat()
        if not _safe_capture_stat(temporary_info):
            raise ValueError("host_capture_invalid")
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("host_capture_invalid")
        if on_publish is not None:
            on_publish(temporary_info)
        os.replace(temporary, path)
        temporary = None
        final_info = path.lstat()
        if (
            not _safe_capture_stat(final_info)
            or not os.path.samestat(temporary_info, final_info)
        ):
            raise ValueError("host_capture_invalid")
    except ValueError:
        raise
    except OSError:
        raise ValueError("host_capture_invalid") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _single_document_source(project: DwsProjectManifest) -> str:
    if (
        not isinstance(project, DwsProjectManifest)
        or len(project.sources) != 1
        or project.sources[0].source_type is not SyncSourceType.DOCUMENT
    ):
        raise ValueError("host_capture_invalid")
    return project.sources[0].source_id


def _metadata_value(
    metadata: Mapping[str, object],
    fields: tuple[str, ...],
) -> object:
    for field in fields:
        if field in metadata:
            return metadata[field]
    return _MISSING


def _normalized_text(value: object, *, max_length: int) -> str | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("host_capture_invalid")
    text = str(value)
    if not text.strip() or len(text) > max_length:
        raise ValueError("host_capture_invalid")
    return text


def _normalized_time(value: object) -> str | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("host_capture_invalid")
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) >= 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise ValueError("host_capture_invalid") from None
    elif isinstance(value, str):
        if not value.strip() or len(value) > 128:
            raise ValueError("host_capture_invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("host_capture_invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("host_capture_invalid")
    else:
        raise ValueError("host_capture_invalid")
    return parsed.astimezone(UTC).isoformat()


def _normalized_document_info(
    document_info: dict[str, object],
    source_id: str,
) -> dict[str, object]:
    try:
        metadata = unwrap_dws_payload(document_info)
    except DwsReadError:
        raise ValueError("host_capture_invalid") from None
    if not isinstance(metadata, Mapping):
        raise ValueError("host_capture_invalid")
    identity_present, identity_matches, metadata_matches = document_metadata_contract(
        metadata,
        source_id,
    )
    if not identity_present or not identity_matches or not metadata_matches:
        raise ValueError("host_capture_invalid")
    result: dict[str, object] = {
        "nodeId": source_id,
        "contentType": "ALIDOC",
        "extension": "adoc",
    }
    title = _normalized_text(
        _metadata_value(metadata, _TITLE_FIELDS),
        max_length=512,
    )
    url = _normalized_text(
        _metadata_value(metadata, _URL_FIELDS),
        max_length=2048,
    )
    version = _normalized_text(
        _metadata_value(metadata, _VERSION_FIELDS),
        max_length=256,
    )
    source_time = _normalized_time(_metadata_value(metadata, _TIME_FIELDS))
    if title is not None:
        result["title"] = title
    if url is not None:
        result["shareUrl"] = url
    if version is not None:
        result["source_version"] = version
    if source_time is not None:
        result["source_time"] = source_time
    return {"result": result}


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ValueError("host_capture_invalid") from None


def _capture_payload(
    project_id: str,
    source_id: str,
    run_token: str,
    document_info: dict[str, object],
) -> bytes:
    return _canonical_json(
        {
            "schema_version": _CAPTURE_SCHEMA_VERSION,
            "project_key": _binding_key("project", project_id),
            "source_key": _binding_key("source", source_id),
            "run_token_key": _binding_key("run-token", run_token),
            "document_info": document_info,
        }
    )


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _document_capture(document_info: dict[str, object]) -> DocumentInfoCapture:
    frozen = _freeze(document_info)
    if not isinstance(frozen, Mapping):
        raise AssertionError("document info must be a mapping")
    return DocumentInfoCapture(cast(Mapping[str, object], frozen))


def _load_protected_capture(
    protected: bytes,
    project: DwsProjectManifest,
    source_id: str,
    run_token: str,
    protector: ContentProtector,
) -> DocumentInfoCapture:
    try:
        plaintext = protector.unprotect(project.project_id, protected)
    except Exception:
        raise ValueError("host_capture_invalid") from None
    if type(plaintext) is not bytes or not plaintext:
        raise ValueError("host_capture_invalid")
    try:
        payload = _decode_json(plaintext)
    except ValueError:
        raise ValueError("host_capture_invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != _CAPTURE_KEYS
        or payload["schema_version"] != _CAPTURE_SCHEMA_VERSION
        or type(payload["schema_version"]) is not int
        or not isinstance(payload["project_key"], str)
        or not isinstance(payload["source_key"], str)
        or not isinstance(payload["run_token_key"], str)
        or not isinstance(payload["document_info"], dict)
    ):
        raise ValueError("host_capture_invalid")
    expected_keys = (
        (payload["project_key"], _binding_key("project", project.project_id)),
        (payload["source_key"], _binding_key("source", source_id)),
        (payload["run_token_key"], _binding_key("run-token", run_token)),
    )
    if any(
        not hmac.compare_digest(actual, expected)
        for actual, expected in expected_keys
    ):
        raise ValueError("host_capture_invalid")
    stored_document_info = payload["document_info"]
    document_info = _normalized_document_info(stored_document_info, source_id)
    if _canonical_json(stored_document_info) != _canonical_json(document_info):
        raise ValueError("host_capture_invalid")
    return _document_capture(document_info)


def _checked_protector(protector: ContentProtector) -> ContentProtector:
    if not callable(getattr(protector, "protect", None)) or not callable(
        getattr(protector, "unprotect", None)
    ):
        raise TypeError("protector must support protect and unprotect")
    return protector


def _validate_discardable_capture(
    project_id: str,
    protected: bytes,
    protector: ContentProtector,
) -> None:
    try:
        plaintext = protector.unprotect(project_id, protected)
    except Exception:
        raise ValueError("host_capture_invalid") from None
    if type(plaintext) is not bytes or not plaintext:
        raise ValueError("host_capture_invalid")
    try:
        payload = _decode_json(plaintext)
    except ValueError:
        raise ValueError("host_capture_invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != _CAPTURE_KEYS
        or payload["schema_version"] != _CAPTURE_SCHEMA_VERSION
        or type(payload["schema_version"]) is not int
        or not isinstance(payload["project_key"], str)
        or not isinstance(payload["source_key"], str)
        or not isinstance(payload["run_token_key"], str)
        or not isinstance(payload["document_info"], dict)
    ):
        raise ValueError("host_capture_invalid")
    stored_document_info = payload["document_info"]
    result = stored_document_info.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("nodeId"), str):
        raise ValueError("host_capture_invalid")
    source_id = result["nodeId"]
    if not source_id or len(source_id) > 512:
        raise ValueError("host_capture_invalid")
    normalized = _normalized_document_info(stored_document_info, source_id)
    if _canonical_json(stored_document_info) != _canonical_json(normalized):
        raise ValueError("host_capture_invalid")
    if any(
        _BINDING_KEY.fullmatch(payload[key]) is None
        for key in ("project_key", "source_key", "run_token_key")
    ):
        raise ValueError("host_capture_invalid")
    if not hmac.compare_digest(
        payload["project_key"],
        _binding_key("project", project_id),
    ):
        raise ValueError("host_capture_invalid")
    if not hmac.compare_digest(
        payload["source_key"],
        _binding_key("source", source_id),
    ):
        raise ValueError("host_capture_invalid")
def prepare_document_info_capture(
    document_info: dict[str, object],
    project: DwsProjectManifest,
    *,
    run_token: str,
    protector: ContentProtector,
    root: Path | None = None,
) -> DocumentInfoCaptureWrite:
    source_id = _single_document_source(project)
    normalized = _normalized_document_info(document_info, source_id)
    selected_root = _capture_root(root)
    selected_protector = _checked_protector(protector)
    return DocumentInfoCaptureWrite(
        project,
        source_id,
        run_token,
        normalized,
        selected_protector,
        selected_root,
    )


def capture_document_info(
    raw: bytes,
    project: DwsProjectManifest,
    *,
    run_token: str,
    protector: ContentProtector,
    root: Path | None = None,
) -> DocumentInfoCapture:
    transaction = prepare_document_info_capture(
        decode_host_result(raw, "doc_info"),
        project,
        run_token=run_token,
        protector=protector,
        root=root,
    )
    transaction.apply()
    return transaction.capture


def prepare_document_info_capture_delete(
    project: DwsProjectManifest,
    *,
    run_token: str,
    protector: ContentProtector,
    root: Path | None = None,
) -> DocumentInfoCaptureDelete:
    source_id = _single_document_source(project)
    return DocumentInfoCaptureDelete(
        project,
        source_id,
        run_token,
        _checked_protector(protector),
        _capture_root(root),
    )


def load_document_info_capture(
    project: DwsProjectManifest,
    *,
    run_token: str,
    protector: ContentProtector,
    root: Path | None = None,
) -> DocumentInfoCapture:
    source_id = _single_document_source(project)
    selected_root = _capture_root(root)
    selected_protector = _checked_protector(protector)
    path = document_info_capture_path(project.project_id)
    _ensure_capture_root(selected_root)
    with state_lock.acquire_state_lock(path, project.project_id, root=selected_root):
        _ensure_capture_root(selected_root)
        protected = _read_capture(path)
        if protected is None:
            raise ValueError("host_capture_missing")
        return _load_protected_capture(
            protected,
            project,
            source_id,
            run_token,
            selected_protector,
        )


def clear_document_info_capture(
    project: DwsProjectManifest,
    *,
    run_token: str,
    protector: ContentProtector,
    root: Path | None = None,
) -> bool:
    source_id = _single_document_source(project)
    selected_root = _capture_root(root)
    selected_protector = _checked_protector(protector)
    path = document_info_capture_path(project.project_id)
    _ensure_capture_root(selected_root)
    with state_lock.acquire_state_lock(path, project.project_id, root=selected_root):
        _ensure_capture_root(selected_root)
        protected = _read_capture(path)
        if protected is None:
            return False
        _load_protected_capture(
            protected,
            project,
            source_id,
            run_token,
            selected_protector,
        )
        try:
            current = path.lstat()
            if not _safe_capture_stat(current):
                raise ValueError("host_capture_invalid")
            path.unlink()
        except ValueError:
            raise
        except OSError:
            raise ValueError("host_capture_invalid") from None
        return True


def discard_document_info_capture(
    project_id: str,
    *,
    protector: ContentProtector,
) -> bool:
    selected_root = _capture_root()
    selected_protector = _checked_protector(protector)
    path = document_info_capture_path(project_id)
    _ensure_capture_root(selected_root)
    with state_lock.acquire_state_lock(path, project_id, root=selected_root):
        _ensure_capture_root(selected_root)
        snapshot = _read_capture_snapshot(path)
        if snapshot is None:
            return False
        protected, path_info = snapshot
        _validate_discardable_capture(
            project_id,
            protected,
            selected_protector,
        )
        return _delete_capture(
            path,
            expected=protected,
            expected_info=path_info,
        )


def document_info_capture_exists(project_id: str) -> bool:
    selected_root = _capture_root()
    path = document_info_capture_path(project_id)
    _ensure_capture_root(selected_root)
    with state_lock.acquire_state_lock(path, project_id, root=selected_root):
        _ensure_capture_root(selected_root)
        return _read_capture(path) is not None


__all__ = [
    "DocumentInfoCapture",
    "DocumentInfoCaptureDelete",
    "DocumentInfoCaptureWrite",
    "capture_document_info",
    "clear_document_info_capture",
    "discard_document_info_capture",
    "document_info_capture_path",
    "document_info_capture_exists",
    "load_document_info_capture",
    "prepare_document_info_capture",
    "prepare_document_info_capture_delete",
]
