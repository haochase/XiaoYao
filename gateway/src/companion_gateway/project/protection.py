from __future__ import annotations

import ctypes
import hashlib
import sys
from collections.abc import Callable
from ctypes import wintypes
from functools import lru_cache
from typing import Protocol, TypeVar


CRYPTPROTECT_UI_FORBIDDEN = 0x1
TOKEN_QUERY = 0x0008
TOKEN_USER = 1


_Result = TypeVar("_Result")


class ProtectionError(RuntimeError):
    pass


class ContentProtector(Protocol):
    def protect(self, project_id: str, plaintext: bytes) -> bytes: ...

    def unprotect(self, project_id: str, protected: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _WindowsApis:
    def __init__(self) -> None:
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        except (AttributeError, OSError):
            _raise_protection_error()

        data_blob_pointer = ctypes.POINTER(_DataBlob)
        self.crypt_protect_data = crypt32.CryptProtectData
        self.crypt_protect_data.argtypes = [
            data_blob_pointer,
            wintypes.LPCWSTR,
            data_blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            data_blob_pointer,
        ]
        self.crypt_protect_data.restype = wintypes.BOOL

        self.crypt_unprotect_data = crypt32.CryptUnprotectData
        self.crypt_unprotect_data.argtypes = [
            data_blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            data_blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            data_blob_pointer,
        ]
        self.crypt_unprotect_data.restype = wintypes.BOOL

        self.local_free = kernel32.LocalFree
        self.local_free.argtypes = [ctypes.c_void_p]
        self.local_free.restype = ctypes.c_void_p

        self.get_current_process = kernel32.GetCurrentProcess
        self.get_current_process.argtypes = []
        self.get_current_process.restype = wintypes.HANDLE

        self.close_handle = kernel32.CloseHandle
        self.close_handle.argtypes = [wintypes.HANDLE]
        self.close_handle.restype = wintypes.BOOL

        self.open_process_token = advapi32.OpenProcessToken
        self.open_process_token.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.open_process_token.restype = wintypes.BOOL

        self.get_token_information = advapi32.GetTokenInformation
        self.get_token_information.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.get_token_information.restype = wintypes.BOOL

        self.convert_sid_to_string_sid = advapi32.ConvertSidToStringSidW
        self.convert_sid_to_string_sid.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.convert_sid_to_string_sid.restype = wintypes.BOOL


@lru_cache(maxsize=1)
def _windows_apis() -> _WindowsApis:
    return _WindowsApis()


def _raise_protection_error() -> None:
    raise ProtectionError("dpapi_operation_failed") from None


def _call_windows_api(
    operation: Callable[..., _Result],
    *arguments: object,
) -> _Result:
    try:
        return operation(*arguments)
    except OSError:
        _raise_protection_error()


def _run_cleanup(operation: Callable[[], None]) -> None:
    primary_exception = sys.exc_info()[0] is not None
    try:
        operation()
    except ProtectionError:
        if not primary_exception:
            raise


def _entropy(project_id: str) -> bytes:
    return hashlib.sha256(
        b"xiaoyao-project-sync-v1\0" + project_id.encode("utf-8")
    ).digest()


def _data_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        ),
        buffer,
    )


def _blob_bytes(blob: _DataBlob) -> bytes:
    if not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _local_free(apis: _WindowsApis, pointer: object) -> None:
    if _call_windows_api(apis.local_free, pointer):
        _raise_protection_error()


def _free_blob(apis: _WindowsApis, blob: _DataBlob) -> None:
    if blob.pbData:
        _local_free(apis, blob.pbData)


def _close_handle(apis: _WindowsApis, handle: wintypes.HANDLE) -> None:
    if not _call_windows_api(apis.close_handle, handle):
        _raise_protection_error()


class WindowsDpapiProtector:
    def protect(self, project_id: str, plaintext: bytes) -> bytes:
        apis = _windows_apis()
        plaintext_blob, _plaintext_buffer = _data_blob(plaintext)
        entropy_blob, _entropy_buffer = _data_blob(_entropy(project_id))
        protected_blob = _DataBlob()
        try:
            protected = _call_windows_api(
                apis.crypt_protect_data,
                ctypes.byref(plaintext_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(protected_blob),
            )
            if not protected:
                _raise_protection_error()
            return _blob_bytes(protected_blob)
        finally:
            _run_cleanup(lambda: _free_blob(apis, protected_blob))

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        apis = _windows_apis()
        protected_blob, _protected_buffer = _data_blob(protected)
        entropy_blob, _entropy_buffer = _data_blob(_entropy(project_id))
        plaintext_blob = _DataBlob()
        try:
            plaintext = _call_windows_api(
                apis.crypt_unprotect_data,
                ctypes.byref(protected_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(plaintext_blob),
            )
            if not plaintext:
                _raise_protection_error()
            return _blob_bytes(plaintext_blob)
        finally:
            _run_cleanup(lambda: _free_blob(apis, plaintext_blob))


def protection_identity_digest() -> str:
    apis = _windows_apis()
    token = wintypes.HANDLE()
    try:
        opened = _call_windows_api(
            apis.open_process_token,
            _call_windows_api(apis.get_current_process),
            TOKEN_QUERY,
            ctypes.byref(token),
        )
        if not opened:
            _raise_protection_error()

        size = wintypes.DWORD()
        _call_windows_api(
            apis.get_token_information,
            token,
            TOKEN_USER,
            None,
            0,
            ctypes.byref(size),
        )
        if not size.value:
            _raise_protection_error()

        token_buffer = (ctypes.c_byte * size.value)()
        received = _call_windows_api(
            apis.get_token_information,
            token,
            TOKEN_USER,
            token_buffer,
            size,
            ctypes.byref(size),
        )
        if not received:
            _raise_protection_error()

        token_user = ctypes.cast(
            token_buffer,
            ctypes.POINTER(_TokenUser),
        ).contents
        sid = wintypes.LPWSTR()
        converted = _call_windows_api(
            apis.convert_sid_to_string_sid,
            token_user.User.Sid,
            ctypes.byref(sid),
        )
        if not converted:
            _raise_protection_error()
        try:
            if sid.value is None:
                _raise_protection_error()
            return hashlib.sha256(sid.value.encode("utf-8")).hexdigest()
        finally:
            _run_cleanup(lambda: _local_free(apis, sid))
    finally:
        if token:
            _run_cleanup(lambda: _close_handle(apis, token))
