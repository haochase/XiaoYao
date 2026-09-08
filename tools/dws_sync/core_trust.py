from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tools.dws_sync.launch import resolve_dws_launch


CORE_APPROVALS_FILE = "approved_dws_cores.json"
CORE_APPROVALS_MAX_BYTES = 65_536
CORE_BINARY_MAX_BYTES = 134_217_728
CORE_TRUST_ERROR = "dws_core_changed_requires_approval"
CORE_ENVIRONMENT_ERROR = "dws_core_environment_invalid"

_FILE_CHUNK_BYTES = 1_048_576
_VERSION_MAX_BYTES = 64
_SIGNATURE_OUTPUT_MAX_BYTES = 8_192
_SIGNATURE_TIMEOUT_SECONDS = 15.0
_REPARSE_POINT_ATTRIBUTE = 0x400
_POWERSHELL = Path(
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)
_SIGNATURE_FIELDS = frozenset(
    {"status", "publisher_name", "signer_thumbprint"}
)
_SHIM_ARCHITECTURES = {
    "cli-common-shim-windows-amd64.exe": "AMD64",
    "cli-common-shim-windows-arm64.exe": "ARM64",
}
_CORE_NAMES = {
    "AMD64": "dws-core-windows-amd64.exe",
    "ARM64": "dws-core-windows-arm64.exe",
}


class ApprovedCoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    architecture: Literal["AMD64", "ARM64"]
    core_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signer_thumbprint: str = Field(pattern=r"^[0-9A-F]{40,128}$")
    publisher_name: Literal["BRIGHT ZENITH PRIVATE LIMITED"]


class ApprovedCoreCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    cores: tuple[ApprovedCoreRecord, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_architectures(self) -> ApprovedCoreCatalog:
        if len({item.architecture for item in self.cores}) != len(self.cores):
            raise ValueError("approved core architectures must be unique")
        return self


@dataclass(frozen=True)
class AuthenticodeDescriptor:
    status: str
    publisher_name: str
    signer_thumbprint: str


@dataclass(frozen=True)
class TrustedDwsCore:
    path: Path
    version: str
    sha256: str
    architecture: Literal["AMD64", "ARM64"]


def _is_reparse(details: os.stat_result) -> bool:
    return bool(
        getattr(details, "st_file_attributes", 0)
        & _REPARSE_POINT_ATTRIBUTE
    )


def _regular_file_details(path: Path) -> os.stat_result:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or _is_reparse(details)
        or details.st_nlink != 1
    ):
        raise ValueError(CORE_TRUST_ERROR)
    return details


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    before = _regular_file_details(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
        os, "O_NOINHERIT", 0
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = _regular_file_details(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not os.path.samestat(before, opened)
            or not os.path.samestat(opened, after)
        ):
            raise ValueError(CORE_TRUST_ERROR)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _read_bounded_regular_file(path: Path, maximum: int) -> bytes:
    descriptor, opened = _open_regular_file(path)
    try:
        if opened.st_size > maximum:
            raise ValueError(CORE_TRUST_ERROR)
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(
                descriptor,
                min(65_536, maximum + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > maximum:
            raise ValueError(CORE_TRUST_ERROR)
        final_opened = os.fstat(descriptor)
        final_path = _regular_file_details(path)
        if (
            total != final_opened.st_size
            or not os.path.samestat(opened, final_opened)
            or not os.path.samestat(final_opened, final_path)
        ):
            raise ValueError(CORE_TRUST_ERROR)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_object(payload: bytes) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        selected: dict[str, object] = {}
        for key, value in pairs:
            if key in selected:
                raise ValueError(CORE_TRUST_ERROR)
            selected[key] = value
        return selected

    def reject_constant(_constant: str) -> None:
        raise ValueError(CORE_TRUST_ERROR)

    decoded = payload.decode("utf-8")
    parsed = json.loads(
        decoded,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError(CORE_TRUST_ERROR)
    return parsed


def _architecture_from_shim(name: str) -> Literal["AMD64", "ARM64"]:
    architecture = _SHIM_ARCHITECTURES.get(name.lower())
    if architecture not in {"AMD64", "ARM64"}:
        raise ValueError(CORE_TRUST_ERROR)
    return architecture


def _core_name(architecture: Literal["AMD64", "ARM64"]) -> str:
    try:
        return _CORE_NAMES[architecture]
    except KeyError:
        raise ValueError(CORE_TRUST_ERROR) from None


def _same_path(left: Path, right: Path) -> bool:
    return (
        left.is_absolute()
        and right.is_absolute()
        and os.path.normcase(str(left)) == os.path.normcase(str(right))
    )


def _read_approvals(path: Path) -> ApprovedCoreCatalog:
    payload = _read_bounded_regular_file(path, CORE_APPROVALS_MAX_BYTES)
    return ApprovedCoreCatalog.model_validate(_json_object(payload))


def _unique_approval(
    catalog: ApprovedCoreCatalog,
    architecture: Literal["AMD64", "ARM64"],
) -> ApprovedCoreRecord:
    matches = [
        record for record in catalog.cores if record.architecture == architecture
    ]
    if len(matches) != 1:
        raise ValueError(CORE_TRUST_ERROR)
    return matches[0]


def _read_version(path: Path) -> str:
    payload = _read_bounded_regular_file(path, _VERSION_MAX_BYTES)
    decoded = payload.decode("ascii")
    version = decoded.rstrip("\r\n")
    if decoded not in {version, f"{version}\n", f"{version}\r\n"}:
        raise ValueError(CORE_TRUST_ERROR)
    if not version or any(
        not part.isascii() or not part.isdigit()
        for part in version.split(".")
    ) or len(version.split(".")) != 3:
        raise ValueError(CORE_TRUST_ERROR)
    return version


def _sha256_regular_file(path: Path) -> str:
    descriptor, opened = _open_regular_file(path)
    digest = hashlib.sha256()
    total = 0
    try:
        if opened.st_size > CORE_BINARY_MAX_BYTES:
            raise ValueError(CORE_TRUST_ERROR)
        while True:
            chunk = os.read(descriptor, _FILE_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > CORE_BINARY_MAX_BYTES:
                raise ValueError(CORE_TRUST_ERROR)
            digest.update(chunk)
        final_opened = os.fstat(descriptor)
        final_path = _regular_file_details(path)
        if (
            total != final_opened.st_size
            or not os.path.samestat(opened, final_opened)
            or not os.path.samestat(final_opened, final_path)
        ):
            raise ValueError(CORE_TRUST_ERROR)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _signature_script() -> Path:
    return (
        Path(__file__).parents[2]
        / "scripts"
        / "read-xiaoqian-dws-core-signature.ps1"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return


def _bounded_signature_output(
    process: subprocess.Popen[bytes],
) -> bytes:
    if process.stdout is None:
        raise ValueError(CORE_TRUST_ERROR)
    output: list[bytes] = []
    read_failed = threading.Event()

    def read_stdout() -> None:
        try:
            value = process.stdout.read(_SIGNATURE_OUTPUT_MAX_BYTES + 1)
        except Exception:
            read_failed.set()
            return
        if isinstance(value, bytes):
            output.append(value)
        else:
            read_failed.set()

    started = time.monotonic()
    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    reader.join(_SIGNATURE_TIMEOUT_SECONDS)
    if reader.is_alive():
        _stop_process(process)
        reader.join(1.0)
        raise ValueError(CORE_TRUST_ERROR)
    if read_failed.is_set() or not output:
        raise ValueError(CORE_TRUST_ERROR)
    remaining = max(
        0.0,
        _SIGNATURE_TIMEOUT_SECONDS - (time.monotonic() - started),
    )
    try:
        returncode = process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        _stop_process(process)
        raise ValueError(CORE_TRUST_ERROR) from None
    if returncode != 0 or len(output[0]) > _SIGNATURE_OUTPUT_MAX_BYTES:
        raise ValueError(CORE_TRUST_ERROR)
    return output[0]


def read_authenticode(core: Path) -> AuthenticodeDescriptor:
    process: subprocess.Popen[bytes] | None = None
    try:
        script = _signature_script()
        if not script.is_absolute() or not _POWERSHELL.is_absolute():
            raise ValueError(CORE_TRUST_ERROR)
        _regular_file_details(script)
        _regular_file_details(core)
        process = subprocess.Popen(
            [
                str(_POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-CorePath",
                str(core),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        output = _bounded_signature_output(process)
        payload = _json_object(output)
        if frozenset(payload) != _SIGNATURE_FIELDS or not all(
            isinstance(payload[field], str) for field in _SIGNATURE_FIELDS
        ):
            raise ValueError(CORE_TRUST_ERROR)
        return AuthenticodeDescriptor(
            status=payload["status"],
            publisher_name=payload["publisher_name"],
            signer_thumbprint=payload["signer_thumbprint"],
        )
    except Exception:
        if process is not None:
            _stop_process(process)
        raise ValueError(CORE_TRUST_ERROR) from None
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def resolve_trusted_dws_core(
    dws_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    official_bin: Path | None = None,
    processor_architecture: str | None = None,
    approvals_path: Path | None = None,
    signature_reader: Callable[[Path], AuthenticodeDescriptor] = read_authenticode,
) -> TrustedDwsCore:
    with hold_trusted_dws_core(
        dws_path,
        environ=environ,
        official_bin=official_bin,
        processor_architecture=processor_architecture,
        approvals_path=approvals_path,
        signature_reader=signature_reader,
    ) as trusted:
        return trusted


def _open_core_read_lock(path: Path) -> int:
    if os.name != "nt":
        raise ValueError(CORE_TRUST_ERROR)

    import msvcrt

    before = _regular_file_details(path)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    # Readers (including Authenticode and CreateProcess) may share this file;
    # writers and replacements must wait until process creation completes.
    handle = create_file(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError(CORE_TRUST_ERROR)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(handle)
        raise
    try:
        opened = os.fstat(descriptor)
        after = _regular_file_details(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or opened.st_nlink != 1
            or not os.path.samestat(before, opened)
            or not os.path.samestat(opened, after)
        ):
            raise ValueError(CORE_TRUST_ERROR)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def hold_trusted_dws_core(
    dws_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    official_bin: Path | None = None,
    processor_architecture: str | None = None,
    approvals_path: Path | None = None,
    signature_reader: Callable[[Path], AuthenticodeDescriptor] = read_authenticode,
) -> Iterator[TrustedDwsCore]:
    descriptor: int | None = None
    try:
        expected_bin = official_bin or (Path.home() / ".qwenworkcn" / "bin")
        wrapper = expected_bin / "dws"
        if not _same_path(dws_path, wrapper):
            raise ValueError(CORE_TRUST_ERROR)
        wrapper_before = _regular_file_details(wrapper)
        shim, _child_env = resolve_dws_launch(
            dws_path,
            environ=environ,
            official_bin=official_bin,
            processor_architecture=processor_architecture,
        )
        wrapper_after = _regular_file_details(wrapper)
        if not os.path.samestat(wrapper_before, wrapper_after):
            raise ValueError(CORE_TRUST_ERROR)
        architecture = _architecture_from_shim(shim.name)
        expected_shim = expected_bin / "ext" / shim.name
        if not _same_path(shim, expected_shim):
            raise ValueError(CORE_TRUST_ERROR)
        _regular_file_details(shim)
        core = shim.parent / _core_name(architecture)
        descriptor = _open_core_read_lock(core)
        catalog = _read_approvals(
            approvals_path or Path(__file__).with_name(CORE_APPROVALS_FILE)
        )
        approval = _unique_approval(catalog, architecture)
        version = _read_version(shim.parent / ".dws-version")
        actual_hash = _sha256_regular_file(core)
        before_signature = _regular_file_details(core)
        signature = signature_reader(core)
        after_signature = _regular_file_details(core)
        verified_hash = _sha256_regular_file(core)
        if (
            not os.path.samestat(before_signature, after_signature)
            or not hmac.compare_digest(actual_hash, verified_hash)
            or approval.core_version != version
            or not hmac.compare_digest(approval.core_sha256, actual_hash)
            or signature.status != "Valid"
            or signature.publisher_name != approval.publisher_name
            or not hmac.compare_digest(
                signature.signer_thumbprint,
                approval.signer_thumbprint,
            )
        ):
            raise ValueError(CORE_TRUST_ERROR)
        trusted = TrustedDwsCore(core, version, actual_hash, architecture)
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, Exception):
            raise ValueError(CORE_TRUST_ERROR) from None
        raise
    try:
        yield trusted
    finally:
        os.close(descriptor)


def _directory_details(path: Path) -> os.stat_result:
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or _is_reparse(details)
    ):
        raise ValueError(CORE_ENVIRONMENT_ERROR)
    return details


def _validate_directory_chain(path: Path) -> None:
    chain = [path, *path.parents]
    for component in reversed(chain):
        _directory_details(component)


def _open_directory(path: Path) -> int:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "unable to open directory")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_verified_directory(path: Path) -> tuple[int, os.stat_result]:
    before = _directory_details(path)
    descriptor = _open_directory(path)
    try:
        opened = os.fstat(descriptor)
        after = _directory_details(path)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _is_reparse(opened)
            or not os.path.samestat(before, opened)
            or not os.path.samestat(opened, after)
        ):
            raise ValueError(CORE_ENVIRONMENT_ERROR)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def prepare_core_temp_directory(runtime_root: Path) -> Path:
    descriptors: list[int] = []
    try:
        if (
            not runtime_root.is_absolute()
            or runtime_root.drive.upper() != "E:"
        ):
            raise ValueError(CORE_ENVIRONMENT_ERROR)
        _validate_directory_chain(runtime_root)
        original_parent = _directory_details(runtime_root.parent)
        original_runtime = _directory_details(runtime_root)
        parent_descriptor, parent_identity = _open_verified_directory(
            runtime_root.parent
        )
        descriptors.append(parent_descriptor)
        runtime_descriptor, runtime_identity = _open_verified_directory(
            runtime_root
        )
        descriptors.append(runtime_descriptor)
        if (
            not os.path.samestat(original_parent, parent_identity)
            or not os.path.samestat(original_runtime, runtime_identity)
        ):
            raise ValueError(CORE_ENVIRONMENT_ERROR)
        temporary = runtime_root / "tmp"
        try:
            before_creation = _directory_details(temporary)
        except FileNotFoundError:
            before_creation = None
        temporary.mkdir(mode=0o700, exist_ok=True)
        created = _directory_details(temporary)
        if before_creation is not None and not os.path.samestat(
            before_creation,
            created,
        ):
            raise ValueError(CORE_ENVIRONMENT_ERROR)
        _validate_directory_chain(temporary)
        temporary_descriptor, temporary_identity = _open_verified_directory(
            temporary
        )
        descriptors.append(temporary_descriptor)
        final_parent = _directory_details(runtime_root.parent)
        final_runtime = _directory_details(runtime_root)
        final_temporary = _directory_details(temporary)
        if (
            not os.path.samestat(parent_identity, final_parent)
            or not os.path.samestat(runtime_identity, final_runtime)
            or not os.path.samestat(created, temporary_identity)
            or not os.path.samestat(temporary_identity, final_temporary)
        ):
            raise ValueError(CORE_ENVIRONMENT_ERROR)
        return temporary
    except Exception:
        raise ValueError(CORE_ENVIRONMENT_ERROR) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
