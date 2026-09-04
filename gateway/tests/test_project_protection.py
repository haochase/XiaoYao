import ctypes
import re
from ctypes import wintypes
from collections.abc import Callable

import pytest

import companion_gateway.project.protection as protection
from companion_gateway.project.protection import (
    ProtectionError,
    WindowsDpapiProtector,
    protection_identity_digest,
)


def assert_sanitized_failure(operation: Callable[[], object]) -> None:
    with pytest.raises(ProtectionError) as error:
        operation()

    assert type(error.value) is ProtectionError
    assert str(error.value) == "dpapi_operation_failed"


class _ProtectedBlobApi:
    def __init__(self, *, fail_local_free: bool = False) -> None:
        self._fail_local_free = fail_local_free
        self._output_buffer = ctypes.create_string_buffer(b"x")

    def crypt_protect_data(self, *_arguments: object) -> bool:
        return self._write_output(_arguments[-1])

    def crypt_unprotect_data(self, *_arguments: object) -> bool:
        return self._write_output(_arguments[-1])

    def local_free(self, _pointer: object) -> None:
        if self._fail_local_free:
            raise OSError("sensitive local free failure")
        return None

    def _write_output(self, output_pointer: object) -> bool:
        output = ctypes.cast(
            output_pointer,
            ctypes.POINTER(protection._DataBlob),
        ).contents
        output.cbData = 1
        output.pbData = ctypes.cast(
            self._output_buffer,
            ctypes.POINTER(ctypes.c_byte),
        )
        return True


class _FailedProtectCleanupApi(_ProtectedBlobApi):
    def crypt_protect_data(self, *_arguments: object) -> bool:
        self._write_output(_arguments[-1])
        return False


class _SidApi:
    def __init__(self, *, failure: str | None = None) -> None:
        self._failure = failure

    def get_current_process(self) -> int:
        return 1

    def open_process_token(
        self,
        _process: object,
        _access: object,
        token_pointer: object,
    ) -> bool:
        self._raise_if("open_process_token")
        token = ctypes.cast(
            token_pointer,
            ctypes.POINTER(wintypes.HANDLE),
        ).contents
        token.value = 1
        return True

    def get_token_information(
        self,
        _token: object,
        _information_class: object,
        token_buffer: object,
        _buffer_size: object,
        return_length_pointer: object,
    ) -> bool:
        self._raise_if("get_token_information")
        return_length = ctypes.cast(
            return_length_pointer,
            ctypes.POINTER(wintypes.DWORD),
        ).contents
        return_length.value = ctypes.sizeof(protection._TokenUser)
        if token_buffer is None:
            return False
        token_user = ctypes.cast(
            token_buffer,
            ctypes.POINTER(protection._TokenUser),
        ).contents
        token_user.User.Sid = 1
        return True

    def convert_sid_to_string_sid(
        self,
        _sid: object,
        sid_pointer: object,
    ) -> bool:
        self._raise_if("convert_sid_to_string_sid")
        output = ctypes.cast(
            sid_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
        ).contents
        output.value = "S-1-5-21-test"
        return True

    def local_free(self, _pointer: object) -> None:
        self._raise_if("local_free")
        return None

    def close_handle(self, _handle: object) -> bool:
        self._raise_if("close_handle")
        return True

    def _raise_if(self, operation: str) -> None:
        if self._failure == operation:
            raise OSError(f"sensitive {operation} failure")


def test_dpapi_round_trip_and_project_entropy_isolation() -> None:
    protector = WindowsDpapiProtector()
    encrypted = protector.protect("project-1", b"private evidence")

    assert encrypted != b"private evidence"
    assert protector.unprotect("project-1", encrypted) == b"private evidence"
    with pytest.raises(ProtectionError, match="^dpapi_operation_failed$"):
        protector.unprotect("project-2", encrypted)


def test_dpapi_rejects_corrupt_ciphertext() -> None:
    with pytest.raises(ProtectionError, match="^dpapi_operation_failed$"):
        WindowsDpapiProtector().unprotect("project-1", b"not-dpapi")


def test_protection_identity_digest_is_a_sha256_digest() -> None:
    digest = protection_identity_digest()

    assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None


def test_protect_native_api_failure_is_sanitized(monkeypatch) -> None:
    class FailingProtectApi:
        def crypt_protect_data(self, *_arguments: object) -> bool:
            raise OSError("sensitive protect failure")

    monkeypatch.setattr(protection, "_windows_apis", lambda: FailingProtectApi())

    assert_sanitized_failure(
        lambda: WindowsDpapiProtector().protect("project-1", b"private evidence")
    )


def test_sid_native_api_failure_is_sanitized(monkeypatch) -> None:
    monkeypatch.setattr(
        protection,
        "_windows_apis",
        lambda: _SidApi(failure="open_process_token"),
    )

    assert_sanitized_failure(protection_identity_digest)


@pytest.mark.parametrize("method_name", ["protect", "unprotect"])
def test_blob_local_free_os_error_is_sanitized(monkeypatch, method_name: str) -> None:
    monkeypatch.setattr(
        protection,
        "_windows_apis",
        lambda: _ProtectedBlobApi(fail_local_free=True),
    )
    protector = WindowsDpapiProtector()

    assert_sanitized_failure(
        lambda: getattr(protector, method_name)("project-1", b"private evidence")
    )


def test_cleanup_failure_does_not_mask_a_dpapi_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        protection,
        "_windows_apis",
        lambda: _FailedProtectCleanupApi(fail_local_free=True),
    )

    with pytest.raises(ProtectionError) as error:
        WindowsDpapiProtector().protect("project-1", b"private evidence")

    assert type(error.value) is ProtectionError
    assert str(error.value) == "dpapi_operation_failed"
    assert error.value.__context__ is None


@pytest.mark.parametrize("failure", ["local_free", "close_handle"])
def test_sid_cleanup_os_error_is_sanitized(monkeypatch, failure: str) -> None:
    monkeypatch.setattr(
        protection,
        "_windows_apis",
        lambda: _SidApi(failure=failure),
    )

    assert_sanitized_failure(protection_identity_digest)
