from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from companion_gateway.project.models import EvidenceRef, ProjectContextPackage
from companion_gateway.project.protection import (
    WindowsDpapiProtector,
    protection_identity_digest,
)
from companion_gateway.project.sync_repository import ProjectSyncRepository
import tools.dws_project_sync as sync_cli
from tools.dws_project_sync import QwenProjectContextArtifact
from tools.dws_sync import (
    DwsRetrievalRequest,
    DwsSourceBundle,
    DwsSourceRecord,
)
from tools.dws_sync import lifecycle
from tools.dws_sync.runtime import prepare_runtime
from tools.tests.dws_core_fixtures import official_installation


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
PROJECT_ID = "project-local-integration"
SCOPE = "project:local-integration"
TOKEN = "local-integration-token"
RUN_LOCAL_INTEGRATION = (
    os.environ.get("COMPANION_RUN_DWS_LOCAL_INTEGRATION") == "1"
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value))


def _write_private_inputs(
    tmp_path: Path,
    *,
    content: str = "Local integration evidence for plan B.",
    version: str = "v1",
    status: str = "active",
    retrieval_requests: tuple[DwsRetrievalRequest, ...] = (),
    completed_request_ids: tuple[str, ...] = (),
) -> dict[str, Path]:
    now = datetime.now(UTC).replace(microsecond=0)
    paths = {
        "manifest": tmp_path / "manifest.json",
        "sources": tmp_path / "sources.json",
        "context": tmp_path / "context.json",
        "state": tmp_path / "state.json",
    }
    _write_json(
        paths["manifest"],
        {
            "schema_version": 1,
            "projects": [
                {
                    "project_id": PROJECT_ID,
                    "project_name": "Local integration project",
                    "profile": "local-test-profile",
                    "permission_scope": SCOPE,
                    "sources": [
                        {
                            "source_type": "document",
                            "source_id": "document-local-1",
                        }
                    ],
                }
            ],
        },
    )
    if status == "active":
        record = DwsSourceRecord(
            source_type="document",
            source_id="document-local-1",
            permission_scope=SCOPE,
            fetched_at=now,
            status="active",
            source_title="Local decision document",
            source_url="dingtalk://doc/document-local-1",
            source_version=version,
            source_time=now,
            content_text=content,
            attributes_json="{}",
            content_hash=_digest(content),
        )
    else:
        record = DwsSourceRecord(
            source_type="document",
            source_id="document-local-1",
            permission_scope=SCOPE,
            fetched_at=now,
            status=status,
        )
    hash_payload = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "project_name": "Local integration project",
        "permission_scope": SCOPE,
        "collected_at": now.isoformat(),
        "records": [record.model_dump(mode="json")],
    }
    if retrieval_requests:
        hash_payload["retrieval_requests"] = [
            item.model_dump(mode="json") for item in retrieval_requests
        ]
    bundle = DwsSourceBundle(
        **hash_payload,
        content_hash=_digest(_canonical(hash_payload).decode("utf-8")),
    )
    _write_json(paths["sources"], bundle.model_dump(mode="json"))
    references = ()
    if status == "active":
        references = (
            EvidenceRef(
                source_type="document",
                source_id="document-local-1",
                source_title="Local decision document",
                source_url="dingtalk://doc/document-local-1",
                source_time=now,
                excerpt=content[: min(32, len(content))],
                permission_scope=SCOPE,
            ),
        )
    context = ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="Local integration project",
        generated_at=now,
        source_refs=references,
        permission_scope=SCOPE,
        freshness_seconds=1_800,
    )
    artifact = QwenProjectContextArtifact(
        schema_version=1,
        context=context,
        completed_retrieval_request_ids=completed_request_ids,
    )
    _write_json(paths["context"], artifact.model_dump(mode="json"))
    return paths


def _environment(tmp_path: Path, database_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "COMPANION_DB_PATH": str(database_path),
            "COMPANION_DWS_SYNC_TOKEN": TOKEN,
            "COMPANION_FEISHU_CHAT_ENABLED": "false",
            "COMPANION_MEETING_ASSISTANT_ENABLED": "false",
            "COMPANION_MEMORY_ENABLED": "false",
            "COMPANION_PROJECT_API_PRINCIPALS": json.dumps(
                {
                    "local-integration": {
                        "token_sha256": _digest(TOKEN),
                        "project_ids": [PROJECT_ID],
                        "permission_scopes": [SCOPE],
                    }
                },
                separators=(",", ":"),
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "COMPANION_VISION_ENABLED": "false",
            "COMPANION_VOICE_RUNTIME": "none",
        }
    )
    return environment


def _wait_http(process: subprocess.Popen[bytes], url: str) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("sync listener exited before readiness")
        try:
            response = opener.open(
                url,
                timeout=0.5,
            )
            with response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError, URLError):
            pass
        time.sleep(0.1)
    raise AssertionError("local listener did not become ready")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: object | None = None,
) -> tuple[int, dict[str, object]]:
    data = _canonical(payload) if payload is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(request, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(65_537)
        assert len(raw) <= 65_536
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        return response.status, parsed


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _sync_port_is_in_use(error: OSError) -> bool:
    return error.errno in {errno.EADDRINUSE, 10048} or getattr(
        error, "winerror", None
    ) == 10048


def test_sync_port_error_classifier_accepts_only_address_in_use() -> None:
    assert _sync_port_is_in_use(OSError(errno.EADDRINUSE, "occupied")) is True
    assert _sync_port_is_in_use(OSError(10048, "occupied")) is True
    windows_error = OSError(0, "occupied")
    windows_error.winerror = 10048
    assert _sync_port_is_in_use(windows_error) is True
    assert _sync_port_is_in_use(OSError(errno.EINVAL, "invalid")) is False


def _require_unused_sync_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8731))
        except OSError as error:
            if _sync_port_is_in_use(error):
                pytest.skip("local sync listener port 8731 is already in use")
            raise


def _run_sync_cli(
    arguments: list[str],
    environment: dict[str, str],
    *,
    input_bytes: bytes | None = None,
    timeout: int = 40,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from pathlib import Path; import sys; "
                "sys.path.insert(0, str(Path.cwd() / 'gateway' / 'src')); "
                "import tools.dws_project_sync as cli; "
                "import tools.dws_sync.host_capture as capture; "
                "import tools.dws_sync.state_lock as lock; "
                "root=Path(sys.argv[1]); cli.LIFECYCLE_ROOT=root; "
                "capture._TEST_CAPTURE_ROOT=root.parent / 'dws-host-captures'; "
                "protector=type('CaptureProtector',(),{"
                "'protect':lambda self,_project,plain:b'test-capture\\0'+plain,"
                "'unprotect':lambda self,_project,protected:protected[len(b'test-capture\\0'):]})(); "
                "cli._host_capture_protector=lambda:protector; "
                "lock.PRIVATE_LOCK_ROOT=root; "
                "from urllib.request import ProxyHandler, build_opener; "
                "raise SystemExit(cli.main(sys.argv[2:], "
                "urlopen=build_opener(ProxyHandler({})).open))"
            ),
            str(Path(environment["TEMP"]) / "dws-sync-locks"),
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        input=input_bytes,
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stdout.decode("utf-8")
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def _run_host_import_process(
    root: Path,
    capture_root: Path,
    lifecycle_root: Path,
    entrypoint: str,
    arguments: list[str],
    environment: dict[str, str],
    *,
    input_bytes: bytes | None = None,
) -> dict[str, object]:
    code = """
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / "gateway" / "src"))

from tools import dws_project_sync as cli
from tools import dws_sync_runtime as runtime
from tools.dws_sync import host_capture, state_lock


class CaptureProtector:
    def protect(self, _project_id, plaintext):
        return b"test-capture\\0" + plaintext

    def unprotect(self, _project_id, protected):
        prefix = b"test-capture\\0"
        if not protected.startswith(prefix):
            raise ValueError("test capture invalid")
        return protected[len(prefix):]


root = Path(sys.argv[1])
capture_root = Path(sys.argv[2])
lifecycle_root = Path(sys.argv[3])
entrypoint = sys.argv[4]
host_capture._TEST_CAPTURE_ROOT = capture_root
cli._host_capture_protector = lambda: CaptureProtector()
cli.LIFECYCLE_ROOT = lifecycle_root
state_lock.PRIVATE_LOCK_ROOT = lifecycle_root
argv = sys.argv[5:]
if entrypoint == "runtime":
    raise SystemExit(runtime.main(argv, root=root))
raise SystemExit(cli.main(argv))
"""
    completed = subprocess.run(
        [
            str(PYTHON),
            "-c",
            code,
            str(root),
            str(capture_root),
            str(lifecycle_root),
            entrypoint,
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        input=input_bytes,
        check=False,
        capture_output=True,
        timeout=40,
    )
    assert completed.returncode == 0, completed.stdout.decode("utf-8")
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def _run_direct_runtime_process(
    root: Path,
    lifecycle_root: Path,
    core: Path,
    arguments: list[str],
    environment: dict[str, str],
    *,
    allow_credential_decrypt: bool,
) -> tuple[int, dict[str, object]]:
    code = """
from contextlib import nullcontext
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / "gateway" / "src"))

from tools import dws_project_sync as cli
from tools import dws_sync_runtime as runtime
from tools.dws_sync import host_capture, state_lock
from tools.dws_sync.core_trust import TrustedDwsCore
from tools.dws_sync import runner as runner_module


class RuntimeProtector:
    def protect(self, _project_id, plaintext):
        return b"runtime-test\\0" + plaintext

    def unprotect(self, _project_id, protected):
        if sys.argv[4] != "1":
            raise AssertionError("credential decrypt forbidden")
        prefix = b"runtime-test\\0"
        if not protected.startswith(prefix):
            raise ValueError("runtime test protection invalid")
        return protected[len(prefix):]


root = Path(sys.argv[1])
lifecycle_root = Path(sys.argv[2])
core = Path(sys.argv[3])
cli.LIFECYCLE_ROOT = lifecycle_root
state_lock.PRIVATE_LOCK_ROOT = lifecycle_root
runtime.WindowsDpapiProtector = lambda: RuntimeProtector()
cli._host_capture_protector = lambda: RuntimeProtector()
host_capture._TEST_CAPTURE_ROOT = root / ".private" / "dws-runtime" / "host-captures"
runner_module._CORE_ENV_ALLOWLIST = runner_module._CORE_ENV_ALLOWLIST | {
    "PATHEXT",
    "SYSTEMROOT",
}
runner_module.hold_trusted_dws_core = lambda *_args, **_kwargs: nullcontext(TrustedDwsCore(
    path=core,
    version="1.0.61",
    sha256="a" * 64,
    architecture="AMD64",
))
raise SystemExit(runtime.main(sys.argv[5:], root=root))
"""
    completed = subprocess.run(
        [
            str(PYTHON),
            "-c",
            code,
            str(root),
            str(lifecycle_root),
            str(core),
            "1" if allow_credential_decrypt else "0",
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        timeout=40,
    )
    assert completed.stderr == b""
    output = json.loads(completed.stdout)
    assert isinstance(output, dict)
    return completed.returncode, output


def _write_fake_direct_core(
    tmp_path: Path,
    *,
    fail: bool,
) -> tuple[Path, Path]:
    _installation, wrapper, _core, _approvals = official_installation(
        tmp_path / ".qwenworkcn"
    )
    launcher = tmp_path / ("fake-core-error.cmd" if fail else "fake-core.cmd")
    info_result = (
        '{"error":{"error_type":"provider_unavailable","retryable":false}}'
        if fail
        else (
            '{"result":{"nodeId":"document-local-1","contentType":"ALIDOC",'
            '"extension":"adoc","title":"Direct document",'
            '"shareUrl":"dingtalk://doc/document-local-1",'
            '"version":"v-direct",'
            '"updatedAt":"2026-09-08T12:00:00+08:00"}}'
        )
    )
    info_exit = 2 if fail else 0
    launcher.write_text(
        "\n".join(
            (
                "@echo off",
                '@if not "%1"=="--profile" exit /b 9',
                '@if not "%2"=="local-test-profile" exit /b 9',
                '@if not "%3"=="doc" exit /b 9',
                '@if not "%5"=="--node" exit /b 9',
                '@if not "%6"=="document-local-1" exit /b 9',
                '@if not "%7"=="--format" exit /b 9',
                '@if not "%8"=="json" exit /b 9',
                '@if "%4"=="info" goto info',
                '@if "%4"=="read" goto read',
                "@exit /b 9",
                ":info",
                f"@echo {info_result}",
                f"@exit /b {info_exit}",
                ":read",
                '@echo {"data":{"markdown":"Direct core content."}}',
                "@exit /b 0",
                "",
            )
        ),
        encoding="ascii",
        newline="\r\n",
    )
    return wrapper, launcher


def _host_result(operation: str, payload: object) -> bytes:
    encoded = _canonical(payload)
    return _canonical(
        {
            "operation": operation,
            "encoding": "base64-json",
            "byte_count": len(encoded),
            "payload": base64.b64encode(encoded).decode("ascii"),
        }
    )


def _host_import_input(markdown: str) -> bytes:
    payloads = (
        (
            "doc_info",
            {
                "result": {
                    "nodeId": "document-local-1",
                    "contentType": "ALIDOC",
                    "extension": "adoc",
                    "title": "Local decision document",
                    "shareUrl": "dingtalk://doc/document-local-1",
                    "version": "v-host",
                    "updatedAt": "2026-09-06T16:30:00+08:00",
                }
            },
        ),
        ("doc_read", {"data": {"markdown": markdown}}),
    )
    results = []
    for operation, payload in payloads:
        encoded = _canonical(payload)
        results.append(
            {
                "operation": operation,
                "encoding": "base64-json",
                "byte_count": len(encoded),
                "payload": base64.b64encode(encoded).decode("ascii"),
            }
        )
    return _canonical(
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "results": results,
        }
    )


def _begin(environment: dict[str, str]) -> str:
    result = _run_sync_cli(
        ["begin", "--project", PROJECT_ID], environment
    )
    assert result["status"] == "started"
    token = result["run_token"]
    assert isinstance(token, str)
    return token


def _lifecycle_root(environment: dict[str, str]) -> Path:
    return Path(environment["TEMP"]) / "dws-sync-locks"


def _push(paths: dict[str, Path], environment: dict[str, str]) -> dict[str, object]:
    token = _begin(environment)
    lifecycle.commit_direct_collection(
        PROJECT_ID,
        token,
        apply=lambda: None,
        rollback=lambda: None,
        root=_lifecycle_root(environment),
    )
    for expected, target in (
        ("collected", "pending"),
        ("pending", "artifact"),
    ):
        lifecycle.advance_run(
            PROJECT_ID,
            token,
            expected=expected,
            target=target,
            root=_lifecycle_root(environment),
        )
    try:
        result = _run_sync_cli(
            [
                "push",
                "--manifest",
                str(paths["manifest"]),
                "--project",
                PROJECT_ID,
                "--sources-file",
                str(paths["sources"]),
                "--context-file",
                str(paths["context"]),
                "--state-file",
                str(paths["state"]),
                "--gateway",
                "http://127.0.0.1:8731",
                "--run-token",
                token,
            ],
            environment,
        )
        ended = _run_sync_cli(
            ["end", "--project", PROJECT_ID, "--run-token", token],
            environment,
        )
        assert ended["status"] == "completed"
        return result
    except BaseException:
        lifecycle.abort_run(
            PROJECT_ID, token, root=_lifecycle_root(environment)
        )
        raise


def _pending(
    paths: dict[str, Path],
    environment: dict[str, str],
) -> dict[str, object]:
    token = _begin(environment)
    lifecycle.commit_direct_collection(
        PROJECT_ID,
        token,
        apply=lambda: None,
        rollback=lambda: None,
        root=_lifecycle_root(environment),
    )
    try:
        return _run_sync_cli(
            [
                "pending",
                "--manifest",
                str(paths["manifest"]),
                "--project",
                PROJECT_ID,
                "--sources-file",
                str(paths["sources"]),
                "--gateway",
                "http://127.0.0.1:8731",
                "--run-token",
                token,
            ],
            environment,
        )
    finally:
        lifecycle.abort_run(
            PROJECT_ID, token, root=_lifecycle_root(environment)
        )


def _start_listener(
    script_name: str,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(PYTHON),
            "-c",
            (
                "import runpy,sys; from pathlib import Path; "
                "sys.path.insert(0,str(Path.cwd() / 'gateway' / 'src')); "
                "sys.path.insert(0,str(Path.cwd() / 'scripts')); "
                "import companion_gateway.settings as settings; "
                "settings.load_environment_file=lambda _path:set(); "
                "sys.argv=sys.argv[1:]; "
                "runpy.run_path(sys.argv[0],run_name='__main__')"
            ),
            str(ROOT / "scripts" / script_name),
            "--gateway-root",
            str(ROOT / "gateway"),
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize(
    "opcode",
    [
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ATTACH,
    ],
)
def test_pending_recovery_sqlite_authorizer_denies_write_opcodes(
    opcode: int,
) -> None:
    assert sync_cli._recovery_sqlite_authorizer(
        opcode, "table", "column", None, None
    ) == sqlite3.SQLITE_DENY


class DirectRuntimeProtector:
    def protect(self, _project_id: str, plaintext: bytes) -> bytes:
        return b"runtime-test\0" + plaintext

    def unprotect(self, _project_id: str, protected: bytes) -> bytes:
        prefix = b"runtime-test\0"
        if not protected.startswith(prefix):
            raise ValueError("runtime test protection invalid")
        return protected[len(prefix) :]


def test_runtime_subprocess_collects_direct_from_fixed_fake_core(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _write_private_inputs(tmp_path)
    wrapper, core = _write_fake_direct_core(tmp_path, fail=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    prepare_runtime(
        tmp_path,
        paths["manifest"],
        PROJECT_ID,
        wrapper,
        DirectRuntimeProtector(),
    )
    environment = _environment(tmp_path, tmp_path / "unused.db")
    environment["USERPROFILE"] = str(tmp_path)
    system_root = environment.pop("SYSTEMROOT", environment.get("SystemRoot", ""))
    environment["SystemRoot"] = system_root
    lifecycle_root = tmp_path / "runtime-lifecycle"

    begin_code, begun = _run_direct_runtime_process(
        tmp_path,
        lifecycle_root,
        core,
        ["begin"],
        environment,
        allow_credential_decrypt=True,
    )
    assert begin_code == 0, begun
    assert begun["status"] == "started"
    token = begun["run_token"]
    assert isinstance(token, str)

    collect_code, collected = _run_direct_runtime_process(
        tmp_path,
        lifecycle_root,
        core,
        ["collect-direct", "--run-token", token],
        environment,
        allow_credential_decrypt=False,
    )

    assert collect_code == 0, collected
    assert collected["status"] == "collected"
    assert collected["active_sources"] == 1
    assert collected["failed_sources"] == 0
    bundle_path = tmp_path / ".private/dws-runtime/source-bundle.json"
    DwsSourceBundle.model_validate_json(bundle_path.read_bytes())
    lifecycle.assert_stage(
        PROJECT_ID,
        token,
        expected="collected",
        root=lifecycle_root,
    )


def test_runtime_subprocess_direct_failure_preserves_bundle_then_aborts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _write_private_inputs(tmp_path)
    wrapper, core = _write_fake_direct_core(tmp_path, fail=True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    prepare_runtime(
        tmp_path,
        paths["manifest"],
        PROJECT_ID,
        wrapper,
        DirectRuntimeProtector(),
    )
    environment = _environment(tmp_path, tmp_path / "unused.db")
    environment["USERPROFILE"] = str(tmp_path)
    system_root = environment.pop("SYSTEMROOT", environment.get("SystemRoot", ""))
    environment["SystemRoot"] = system_root
    lifecycle_root = tmp_path / "runtime-lifecycle"
    bundle_path = tmp_path / ".private/dws-runtime/source-bundle.json"
    old_bundle = b"previous-good-bundle"
    bundle_path.write_bytes(old_bundle)
    begin_code, begun = _run_direct_runtime_process(
        tmp_path,
        lifecycle_root,
        core,
        ["begin"],
        environment,
        allow_credential_decrypt=True,
    )
    assert begin_code == 0, begun
    token = begun["run_token"]
    assert isinstance(token, str)

    collect_code, failed = _run_direct_runtime_process(
        tmp_path,
        lifecycle_root,
        core,
        ["collect-direct", "--run-token", token],
        environment,
        allow_credential_decrypt=False,
    )

    assert collect_code == 1
    assert failed == {"status": "error", "error_type": "provider_unavailable"}
    assert bundle_path.read_bytes() == old_bundle
    lifecycle.assert_stage(
        PROJECT_ID,
        token,
        expected="begun",
        root=lifecycle_root,
    )

    abort_code, aborted = _run_direct_runtime_process(
        tmp_path,
        lifecycle_root,
        core,
        ["abort", "--run-token", token],
        environment,
        allow_credential_decrypt=True,
    )
    assert abort_code == 0
    assert aborted == {"status": "aborted", "project_id": PROJECT_ID}


def test_host_import_subprocess_fixture_preserves_unicode(
    tmp_path: Path,
) -> None:
    paths = _write_private_inputs(tmp_path)
    paths["sources"].unlink()
    environment = _environment(tmp_path, tmp_path / "unused.db")
    token = _begin(environment)
    markdown = '# 决策\n采用方案 B，包含中文与 "引号"。'

    result = _run_sync_cli(
        [
            "host-import",
            "--manifest",
            str(paths["manifest"]),
            "--project",
            PROJECT_ID,
            "--output",
            str(paths["sources"]),
            "--run-token",
            token,
        ],
        environment,
        input_bytes=_host_import_input(markdown),
    )

    assert result["status"] == "collected"
    assert result["source_count"] == 1
    bundle = DwsSourceBundle.model_validate_json(paths["sources"].read_bytes())
    assert bundle.records[0].status == "active"
    assert bundle.records[0].content_text == markdown


def test_two_runtime_subprocesses_complete_host_import_and_abort_crash_capture(
    tmp_path: Path,
) -> None:
    class RuntimeProtector:
        def protect(self, _project_id: str, plaintext: bytes) -> bytes:
            return b"runtime-test\0" + plaintext

        def unprotect(self, _project_id: str, protected: bytes) -> bytes:
            prefix = b"runtime-test\0"
            if not protected.startswith(prefix):
                raise ValueError("runtime test protection invalid")
            return protected[len(prefix) :]

    paths = _write_private_inputs(tmp_path)
    dws = tmp_path / "dws.exe"
    dws.write_bytes(b"test fixture")
    prepare_runtime(
        tmp_path,
        paths["manifest"],
        PROJECT_ID,
        dws,
        RuntimeProtector(),
    )
    environment = _environment(tmp_path, tmp_path / "unused.db")
    lifecycle_root = tmp_path / "runtime-locks"
    capture_root = tmp_path / "runtime-captures"
    started = lifecycle.begin_run(PROJECT_ID, root=lifecycle_root)
    token = started.run_token
    assert started.status == "started"
    assert isinstance(token, str)
    info = {
        "result": {
            "nodeId": "document-local-1",
            "contentType": "ALIDOC",
            "extension": "adoc",
            "title": "中文 \"标题\"",
            "shareUrl": "dingtalk://doc/document-local-1",
            "version": "v-host",
            "updatedAt": "2026-09-06T16:30:00+08:00",
        }
    }
    markdown = '# 中文标题\n包含 "引号"、反斜杠 C:\\临时\\文档，以及第二行。'

    captured = _run_host_import_process(
        tmp_path,
        capture_root,
        lifecycle_root,
        "runtime",
        ["capture-info", "--run-token", token],
        environment,
        input_bytes=_host_result("doc_info", info),
    )
    assert captured == {
        "status": "host_info_captured",
        "project_id": PROJECT_ID,
    }
    completed = _run_host_import_process(
        tmp_path,
        capture_root,
        lifecycle_root,
        "runtime",
        ["complete-host-import", "--run-token", token],
        environment,
        input_bytes=_host_result("doc_read", {"data": {"markdown": markdown}}),
    )
    assert completed["status"] == "collected"
    bundle_path = tmp_path / ".private/dws-runtime/source-bundle.json"
    bundle = DwsSourceBundle.model_validate_json(bundle_path.read_bytes())
    assert bundle.records[0].content_text == markdown
    assert not any(capture_root.glob("*.dpapi"))
    lifecycle.assert_stage(
        PROJECT_ID,
        token,
        expected="collected",
        root=lifecycle_root,
    )

    crash_root = tmp_path / "crash"
    crash_root.mkdir()
    crash_paths = _write_private_inputs(crash_root)
    crash_dws = crash_root / "dws.exe"
    crash_dws.write_bytes(b"test fixture")
    prepare_runtime(
        crash_root,
        crash_paths["manifest"],
        PROJECT_ID,
        crash_dws,
        RuntimeProtector(),
    )
    crash_environment = _environment(crash_root, crash_root / "unused.db")
    crash_lifecycle_root = crash_root / "runtime-locks"
    crash_capture_root = crash_root / "runtime-captures"
    crash_started = lifecycle.begin_run(PROJECT_ID, root=crash_lifecycle_root)
    crash_token = crash_started.run_token
    assert isinstance(crash_token, str)

    assert _run_host_import_process(
        crash_root,
        crash_capture_root,
        crash_lifecycle_root,
        "runtime",
        ["capture-info", "--run-token", crash_token],
        crash_environment,
        input_bytes=_host_result("doc_info", info),
    )["status"] == "host_info_captured"
    assert any(crash_capture_root.glob("*.dpapi"))
    crash_bundle = crash_root / ".private/dws-runtime/source-bundle.json"
    assert not crash_bundle.exists()

    aborted = _run_host_import_process(
        crash_root,
        crash_capture_root,
        crash_lifecycle_root,
        "cli",
        ["abort", "--project", PROJECT_ID, "--run-token", crash_token],
        crash_environment,
    )
    assert aborted == {"status": "aborted", "project_id": PROJECT_ID}
    assert not any(crash_capture_root.glob("*.dpapi"))
    assert not crash_bundle.exists()
    state = lifecycle._read_state(
        lifecycle.project_state_path(crash_lifecycle_root, PROJECT_ID),
        PROJECT_ID,
    )
    assert state is not None
    assert state["active"] is False
    assert state["stage"] == "aborted"


@pytest.mark.skipif(
    not RUN_LOCAL_INTEGRATION,
    reason="requires explicit same-user Windows DPAPI and local listener run",
)
def test_live_cli_to_sync_listener_applied_then_unchanged(tmp_path: Path) -> None:
    _require_unused_sync_port()
    assert tmp_path.drive.upper() == "E:"
    database_path = tmp_path / "live-sync.db"
    paths = _write_private_inputs(tmp_path)
    environment = _environment(tmp_path, database_path)
    server = _start_listener("run_xiaoyao_sync.py", environment)
    try:
        _wait_http(server, "http://127.0.0.1:8731/ready")
        first = _push(paths, environment)
        second = _push(paths, environment)
    finally:
        _stop_process(server)

    assert first["outcome"] == "applied"
    assert second["outcome"] == "unchanged"
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    assert state["last_cursor"] == 2
    assert state["pending"] is None

    repository = ProjectSyncRepository(database_path)
    repository.initialize()
    protector = WindowsDpapiProtector()
    repository.configure_protection(
        protection_identity_digest(),
        protector.protector_version,
    )
    active = repository.load_active_generation(PROJECT_ID)
    assert active is not None
    assert active.source_cursor == 2
    assert len(active.protected_chunks) == 1
    ciphertext = active.protected_chunks[0].protected_text
    plaintext = protector.unprotect(PROJECT_ID, ciphertext)
    assert plaintext == b"Local integration evidence for plan B."
    assert plaintext not in ciphertext
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_sync_audits"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM project_sync_generations"
        ).fetchone() == (1,)


@pytest.mark.skipif(
    not RUN_LOCAL_INTEGRATION,
    reason="requires explicit independent local listeners and same-user DPAPI",
)
def test_live_device_and_sync_processes_share_authoritative_sqlite(
    tmp_path: Path,
) -> None:
    _require_unused_sync_port()
    assert tmp_path.drive.upper() == "E:"
    database_path = tmp_path / "live-shared.db"
    paths = _write_private_inputs(tmp_path)
    environment = _environment(tmp_path, database_path)
    device_port = _free_loopback_port()
    device_base = f"http://127.0.0.1:{device_port}"
    sync = _start_listener("run_xiaoyao_sync.py", environment)
    device = _start_listener(
        "run_xiaoyao_gateway.py",
        environment,
        "--host",
        "127.0.0.1",
        "--port",
        str(device_port),
    )
    try:
        _wait_http(sync, "http://127.0.0.1:8731/ready")
        _wait_http(device, f"{device_base}/health")
        openapi_status, openapi = _request_json(
            f"{device_base}/openapi.json"
        )
        assert openapi_status == 200
        assert "/v1/projects/{project_id}/query" in openapi["paths"]
        assert _push(paths, environment)["outcome"] == "applied"
        first_status, first = _request_json(
            f"{device_base}/v1/projects/{PROJECT_ID}/query",
            method="POST",
            payload={"query": "Local integration evidence", "kind": "fact"},
        )
        assert first_status == 200, first
        assert "Local integration evidence" in first["answer"]["text"]

        paths = _write_private_inputs(
            tmp_path,
            content="Updated evidence is visible across processes.",
            version="v2",
        )
        assert _push(paths, environment)["outcome"] == "applied"
        updated_status, updated = _request_json(
            f"{device_base}/v1/projects/{PROJECT_ID}/query",
            method="POST",
            payload={"query": "Updated evidence", "kind": "fact"},
        )
        assert updated_status == 200
        assert "Updated evidence" in updated["answer"]["text"]

        paths = _write_private_inputs(
            tmp_path,
            status="revoked",
            version="v3",
        )
        assert _push(paths, environment)["project_status"] == "stale"
        revoked_status, revoked = _request_json(
            f"{device_base}/v1/projects/{PROJECT_ID}/query",
            method="POST",
            payload={"query": "Updated evidence", "kind": "fact"},
        )
        assert revoked_status == 404
        assert revoked["detail"] == "source_stale"

        _stop_process(sync)
        sync = _start_listener("run_xiaoyao_sync.py", environment)
        _wait_http(sync, "http://127.0.0.1:8731/ready")
        status_code, status = _request_json(
            f"http://127.0.0.1:8731/v1/projects/{PROJECT_ID}/sync/status"
        )
        assert status_code == 200
        assert status["status"]["health"] == "stale"

        paths = _write_private_inputs(
            tmp_path,
            content="Existing retrieval anchor.",
            version="v3",
        )
        assert _push(paths, environment)["outcome"] == "applied"
        missing_status, missing = _request_json(
            f"{device_base}/v1/projects/{PROJECT_ID}/query",
            method="POST",
            payload={"query": "需要补充的唯一证据", "kind": "fact"},
        )
        assert missing_status == 404
        assert missing["detail"] == "evidence_pending"

        pending = _pending(paths, environment)
        assert pending["request_count"] == 1
        bundle = DwsSourceBundle.model_validate_json(
            paths["sources"].read_text(encoding="utf-8")
        )
        retrieval = bundle.retrieval_requests[0]
        paths = _write_private_inputs(
            tmp_path,
            content="需要补充的唯一证据已经取得。",
            version="v4",
            retrieval_requests=(retrieval,),
            completed_request_ids=(retrieval.request_id,),
        )
        assert _push(paths, environment)["outcome"] == "applied"
        request_status, request = _request_json(
            f"http://127.0.0.1:8731/v1/projects/{PROJECT_ID}/"
            f"retrieval-requests/{retrieval.request_id}"
        )
        assert request_status == 200
        assert request["request"]["status"] == "completed"
        answered_status, answered = _request_json(
            f"{device_base}/v1/projects/{PROJECT_ID}/query",
            method="POST",
            payload={"query": "需要补充的唯一证据", "kind": "fact"},
        )
        assert answered_status == 200
        assert "需要补充的唯一证据" in answered["answer"]["text"]
    finally:
        _stop_process(device)
        _stop_process(sync)
