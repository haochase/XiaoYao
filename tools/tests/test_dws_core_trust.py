from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from tools.dws_sync import core_trust
from tools.dws_sync.core_trust import (
    CORE_APPROVALS_MAX_BYTES,
    AuthenticodeDescriptor,
    prepare_core_temp_directory,
    read_authenticode,
    resolve_trusted_dws_core,
)
from tools.tests.dws_core_fixtures import official_installation, write_approval


TRUST_ERROR = "^dws_core_changed_requires_approval$"


def valid_signature(_path: Path) -> AuthenticodeDescriptor:
    return AuthenticodeDescriptor(
        status="Valid",
        publisher_name="BRIGHT ZENITH PRIVATE LIMITED",
        signer_thumbprint="D" * 40,
    )


def resolve_fixture(
    installation: Path,
    wrapper: Path,
    approvals: Path,
    *,
    signature_reader=valid_signature,
):
    return resolve_trusted_dws_core(
        wrapper,
        environ={
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "USERPROFILE": "C:/Users/test",
        },
        official_bin=installation,
        processor_architecture="AMD64",
        approvals_path=approvals,
        signature_reader=signature_reader,
    )


def test_resolver_returns_only_pinned_official_sibling_core(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core, version="1.0.61")

    selected = resolve_fixture(installation, wrapper, approvals)

    assert selected.path == core
    assert selected.version == "1.0.61"
    assert selected.sha256 == hashlib.sha256(core.read_bytes()).hexdigest()
    assert selected.architecture == "AMD64"


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing contract")
def test_resolver_rejects_core_with_existing_writer(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)

    with core.open("r+b"):
        with pytest.raises(ValueError, match=TRUST_ERROR):
            resolve_fixture(installation, wrapper, approvals)

    assert resolve_fixture(installation, wrapper, approvals).path == core


def test_resolver_releases_lock_after_signature_failure(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)

    def failed_signature(_path: Path) -> AuthenticodeDescriptor:
        raise RuntimeError("signature failure")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(
            installation, wrapper, approvals, signature_reader=failed_signature
        )
    core.write_bytes(b"replacement after failed verification")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("core_sha256", "0" * 64),
        ("core_version", "1.0.60"),
        ("architecture", "ARM64"),
        ("extra", "forbidden"),
    ],
)
def test_resolver_fails_closed_for_untrusted_installation(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core, version="1.0.61")
    payload = json.loads(approvals.read_text(encoding="utf-8"))
    payload["cores"][0][field] = value
    approvals.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_missing_core(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    core.unlink()

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_modified_official_wrapper(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    wrapper.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_direct_official_shim_input(tmp_path: Path) -> None:
    installation, _wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    shim = installation / "ext" / "cli-common-shim-windows-amd64.exe"

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, shim, approvals)


def test_resolver_rejects_nonofficial_shim_input(tmp_path: Path) -> None:
    installation, _wrapper, _core, approvals = official_installation(tmp_path)
    rogue = tmp_path / "rogue" / "ext"
    rogue.mkdir(parents=True)
    shim = rogue / "cli-common-shim-windows-amd64.exe"
    shim.write_bytes(b"rogue-shim")
    (rogue / ".dws-version").write_text("1.0.61\n", encoding="ascii")
    core = rogue / "dws-core-windows-amd64.exe"
    core.write_bytes(b"signed-core-fixture")
    write_approval(approvals, core)

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, shim, approvals)


def test_resolver_rejects_shim_beside_tampered_wrapper(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    wrapper.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    shim = installation / "ext" / "cli-common-shim-windows-amd64.exe"

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, shim, approvals)


def test_resolver_rejects_unexpected_resolved_shim_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation, wrapper, _core, approvals = official_installation(tmp_path)
    rogue = tmp_path / "rogue" / "ext"
    rogue.mkdir(parents=True)
    shim = rogue / "cli-common-shim-windows-amd64.exe"
    shim.write_bytes(b"rogue-shim")
    (rogue / ".dws-version").write_text("1.0.61\n", encoding="ascii")
    core = rogue / "dws-core-windows-amd64.exe"
    core.write_bytes(b"signed-core-fixture")
    write_approval(approvals, core)
    monkeypatch.setattr(
        core_trust,
        "resolve_dws_launch",
        lambda *_args, **_kwargs: (shim, {}),
    )

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


@pytest.mark.parametrize("linked_part", ["wrapper", "shim"])
def test_resolver_rejects_hardlinked_launch_files(
    tmp_path: Path,
    linked_part: str,
) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    target = (
        wrapper
        if linked_part == "wrapper"
        else installation / "ext" / "cli-common-shim-windows-amd64.exe"
    )
    try:
        os.link(target, target.with_name(f"{target.name}.second-link"))
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_duplicate_approval_keys(tmp_path: Path) -> None:
    installation, wrapper, _core, approvals = official_installation(tmp_path)
    approvals.write_text(
        '{"schema_version":1,"schema_version":1,"cores":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_duplicate_approval_architectures(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    payload = json.loads(approvals.read_text(encoding="utf-8"))
    payload["cores"].append(dict(payload["cores"][0]))
    approvals.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_oversized_approval_file(tmp_path: Path) -> None:
    installation, wrapper, _core, approvals = official_installation(tmp_path)
    approvals.write_bytes(b" " * (CORE_APPROVALS_MAX_BYTES + 1))

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_symlink_core(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    target = core.with_name("actual-core.exe")
    core.replace(target)
    try:
        core.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    write_approval(approvals, target)

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


def test_resolver_rejects_hardlinked_core(tmp_path: Path) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)
    try:
        os.link(core, core.with_name("second-core-link.exe"))
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(installation, wrapper, approvals)


@pytest.mark.parametrize(
    ("status", "publisher", "thumbprint"),
    [
        ("NotSigned", "BRIGHT ZENITH PRIVATE LIMITED", "D" * 40),
        ("Valid", "Different Publisher", "D" * 40),
        ("Valid", "BRIGHT ZENITH PRIVATE LIMITED", "E" * 40),
    ],
)
def test_resolver_rejects_invalid_live_authenticode(
    tmp_path: Path,
    status: str,
    publisher: str,
    thumbprint: str,
) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(
            installation,
            wrapper,
            approvals,
            signature_reader=lambda _path: AuthenticodeDescriptor(
                status=status,
                publisher_name=publisher,
                signer_thumbprint=thumbprint,
            ),
        )


def test_signature_script_is_read_only_and_uses_literal_path() -> None:
    script = (
        Path(__file__).parents[2]
        / "scripts"
        / "read-xiaoqian-dws-core-signature.ps1"
    )
    content = script.read_text(encoding="utf-8")

    assert "Import-Module" in content
    assert "Microsoft.PowerShell.Security.psd1" in content
    assert "Get-AuthenticodeSignature" in content
    assert "-LiteralPath" in content
    for forbidden in (
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Set-Content",
        "Add-Content",
        "Out-File",
        "COMPANION_",
        " dws ",
    ):
        assert forbidden not in content


class SignatureProcess:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout = BytesIO(stdout)
        self.returncode = returncode
        self.wait_calls: list[float | None] = []
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class BlockingSignatureStdout:
    def __init__(self) -> None:
        self.released = threading.Event()
        self.closed = False

    def read(self, _size: int) -> bytes:
        self.released.wait(5)
        return b""

    def close(self) -> None:
        self.closed = True


class BlockingSignatureProcess(SignatureProcess):
    def __init__(self) -> None:
        super().__init__(b"")
        self.stdout = BlockingSignatureStdout()

    def kill(self) -> None:
        self.killed = True
        self.stdout.released.set()


def test_authenticode_reader_bounds_stdout_read_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = tmp_path / "dws-core-windows-amd64.exe"
    core.write_bytes(b"core")
    process = BlockingSignatureProcess()
    monkeypatch.setattr(
        core_trust.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(core_trust, "_SIGNATURE_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    with pytest.raises(ValueError, match=TRUST_ERROR):
        read_authenticode(core)

    assert time.monotonic() - started < 1.0
    assert process.killed is True
    assert process.stdout.closed is True


def test_resolver_rehashes_core_after_signature_check(
    tmp_path: Path,
) -> None:
    installation, wrapper, core, approvals = official_installation(tmp_path)
    write_approval(approvals, core)

    def replace_core(_path: Path) -> AuthenticodeDescriptor:
        core.write_bytes(b"different-signed-core-fixture")
        return valid_signature(core)

    with pytest.raises(ValueError, match=TRUST_ERROR):
        resolve_fixture(
            installation,
            wrapper,
            approvals,
            signature_reader=replace_core,
        )


def test_authenticode_reader_uses_fixed_powershell_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = tmp_path / "dws-core-windows-amd64.exe"
    core.write_bytes(b"core")
    process = SignatureProcess(
        json.dumps(
            {
                "status": "Valid",
                "publisher_name": "BRIGHT ZENITH PRIVATE LIMITED",
                "signer_thumbprint": "D" * 40,
            }
        ).encode("utf-8")
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **options: object) -> SignatureProcess:
        calls.append((argv, options))
        return process

    monkeypatch.setattr(core_trust.subprocess, "Popen", fake_popen)

    descriptor = read_authenticode(core)

    signature_script = (
        Path(core_trust.__file__).parents[2]
        / "scripts"
        / "read-xiaoqian-dws-core-signature.ps1"
    )
    assert calls[0][0] == [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(signature_script),
        "-CorePath",
        str(core),
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert descriptor.status == "Valid"


def test_prepare_core_temp_directory_accepts_only_e_drive_runtime(
    tmp_path: Path,
) -> None:
    if tmp_path.drive.upper() != "E:":
        pytest.skip("test requires the planned E-drive pytest base")
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    selected = prepare_core_temp_directory(runtime)

    assert selected == runtime / "tmp"
    assert selected.is_dir()
    with pytest.raises(ValueError, match="^dws_core_environment_invalid$"):
        prepare_core_temp_directory(Path(r"C:\xiaoqian-runtime"))


@pytest.mark.parametrize("invalid_part", ["runtime", "tmp"])
def test_prepare_core_temp_directory_rejects_non_directory_components(
    tmp_path: Path,
    invalid_part: str,
) -> None:
    if tmp_path.drive.upper() != "E:":
        pytest.skip("test requires the planned E-drive pytest base")
    runtime = tmp_path / "runtime"
    if invalid_part == "runtime":
        runtime.write_text("not a directory", encoding="ascii")
    else:
        runtime.mkdir()
        (runtime / "tmp").write_text("not a directory", encoding="ascii")

    with pytest.raises(ValueError, match="^dws_core_environment_invalid$"):
        prepare_core_temp_directory(runtime)


def test_prepare_core_temp_directory_rejects_non_directory_parent(
    tmp_path: Path,
) -> None:
    if tmp_path.drive.upper() != "E:":
        pytest.skip("test requires the planned E-drive pytest base")
    parent = tmp_path / "parent"
    parent.write_text("not a directory", encoding="ascii")

    with pytest.raises(ValueError, match="^dws_core_environment_invalid$"):
        prepare_core_temp_directory(parent / "runtime")


def make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("directory link creation is unavailable")


@pytest.mark.parametrize("linked_part", ["parent", "runtime", "tmp"])
def test_prepare_core_temp_directory_rejects_linked_components(
    tmp_path: Path,
    linked_part: str,
) -> None:
    if tmp_path.drive.upper() != "E:":
        pytest.skip("test requires the planned E-drive pytest base")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent = tmp_path / "parent"
    runtime = parent / "runtime"
    if linked_part == "parent":
        make_directory_link(parent, real_parent)
        runtime.mkdir()
    else:
        parent.mkdir()
        if linked_part == "runtime":
            real_runtime = real_parent / "runtime"
            real_runtime.mkdir()
            make_directory_link(runtime, real_runtime)
        else:
            runtime.mkdir()
            real_tmp = real_parent / "tmp"
            real_tmp.mkdir()
            make_directory_link(runtime / "tmp", real_tmp)

    with pytest.raises(ValueError, match="^dws_core_environment_invalid$"):
        prepare_core_temp_directory(runtime)


@pytest.mark.parametrize(
    "replacement_phase",
    ["parent", "runtime", "tmp-before-open", "tmp-after-open"],
)
def test_prepare_core_temp_directory_rejects_real_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_phase: str,
) -> None:
    if tmp_path.drive.upper() != "E:":
        pytest.skip("test requires the planned E-drive pytest base")
    parent = tmp_path / "parent"
    runtime = parent / "runtime"
    temporary = runtime / "tmp"
    runtime.mkdir(parents=True)
    if replacement_phase != "tmp-before-open":
        temporary.mkdir()
    original_open_directory = core_trust._open_directory
    replaced = False

    def replace_directory(path: Path) -> None:
        displaced = path.with_name(f"{path.name}-displaced")
        path.rename(displaced)
        path.mkdir(parents=True)

    def replacing_open_directory(path: Path) -> int:
        nonlocal replaced
        if path != temporary or replaced:
            return original_open_directory(path)
        replaced = True
        if replacement_phase == "parent":
            displaced = parent.with_name("parent-displaced")
            parent.rename(displaced)
            parent.mkdir()
            (displaced / "runtime").rename(runtime)
        elif replacement_phase == "runtime":
            displaced = runtime.with_name("runtime-displaced")
            runtime.rename(displaced)
            runtime.mkdir()
            (displaced / "tmp").rename(temporary)
        elif replacement_phase == "tmp-before-open":
            replace_directory(temporary)
        descriptor = original_open_directory(path)
        if replacement_phase == "tmp-after-open":
            replace_directory(temporary)
        return descriptor

    monkeypatch.setattr(
        core_trust,
        "_open_directory",
        replacing_open_directory,
    )

    with pytest.raises(ValueError, match="^dws_core_environment_invalid$"):
        prepare_core_temp_directory(runtime)
    assert replaced is True
