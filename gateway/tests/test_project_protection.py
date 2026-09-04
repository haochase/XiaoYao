import re

import pytest

from companion_gateway.project.protection import (
    ProtectionError,
    WindowsDpapiProtector,
    protection_identity_digest,
)


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
