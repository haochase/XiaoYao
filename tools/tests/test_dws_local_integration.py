from __future__ import annotations

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
from tools.dws_project_sync import QwenProjectContextArtifact
from tools.dws_sync import (
    DwsRetrievalRequest,
    DwsSourceBundle,
    DwsSourceRecord,
)


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


def _push(paths: dict[str, Path], environment: dict[str, str]) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(PYTHON),
            "-m",
            "tools.dws_project_sync",
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
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        timeout=40,
    )
    assert completed.returncode == 0, completed.stdout.decode("utf-8")
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def _pending(
    paths: dict[str, Path],
    environment: dict[str, str],
) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(PYTHON),
            "-m",
            "tools.dws_project_sync",
            "pending",
            "--manifest",
            str(paths["manifest"]),
            "--project",
            PROJECT_ID,
            "--sources-file",
            str(paths["sources"]),
            "--gateway",
            "http://127.0.0.1:8731",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        timeout=40,
    )
    assert completed.returncode == 0, completed.stdout.decode("utf-8")
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def _start_listener(
    script_name: str,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(PYTHON),
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


@pytest.mark.skipif(
    not RUN_LOCAL_INTEGRATION,
    reason="requires explicit same-user Windows DPAPI and local listener run",
)
def test_live_cli_to_sync_listener_applied_then_unchanged(tmp_path: Path) -> None:
    assert tmp_path.drive.upper() == "E:"
    database_path = tmp_path / "live-sync.db"
    paths = _write_private_inputs(tmp_path)
    environment = _environment(tmp_path, database_path)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    server = subprocess.Popen(
        [
            str(PYTHON),
            str(ROOT / "scripts" / "run_xiaoyao_sync.py"),
            "--gateway-root",
            str(ROOT / "gateway"),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
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
