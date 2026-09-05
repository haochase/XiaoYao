from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from companion_gateway.project.auth import (
    ProjectApiAuthenticator,
    ProjectApiPrincipal,
    ProjectAuthenticationError,
    ProjectAuthorizationError,
)
from companion_gateway.project.index import ProjectSnapshotRegistry
from companion_gateway.project.models import EvidenceRef
from companion_gateway.project.protection import (
    WindowsDpapiProtector,
    protection_identity_digest,
)
from companion_gateway.project.sync_models import (
    RetrievalRequest,
    RetrievalRequestStatus,
    SyncEnvelope,
)
from companion_gateway.project.sync_repository import (
    ProjectSyncRepository,
    SyncConflict,
)
from companion_gateway.project.sync_service import (
    ProjectSourceUnavailable,
    ProjectSyncService,
    ProjectSyncValidationError,
)
from companion_gateway.settings import Settings, load_environment_file


_PROXY_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-original-url",
    }
)
_ALLOWED_HOSTS = frozenset({"127.0.0.1:8731", "localhost:8731"})
_SAFE_SYNC_VALIDATION_ERRORS = frozenset(
    {
        "clock_skew_exceeded",
        "content_hash_mismatch",
        "context_fact_unreferenced",
        "invalid_envelope",
        "now_must_be_aware",
        "source_excerpt_mismatch",
        "source_ref_mismatch",
    }
)
_SAFE_SOURCE_ERRORS = frozenset(
    {
        "clock_untrusted",
        "project_not_synced",
        "source_stale",
        "source_unavailable",
    }
)
_SAFE_CONFLICT_ERRORS = frozenset(
    {
        "context_conflict",
        "cursor_content_conflict",
        "retrieval_evidence_missing",
        "retrieval_request_conflict",
        "stale_cursor",
    }
)
LOCAL_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_SYNC_INTERVAL_SECONDS = 300.0


class RetrievalRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=30)


class _SyncBoundaryMiddleware:
    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = scope.get("headers", ())
        header_names = {name.lower() for name, _ in headers}
        if header_names & _PROXY_HEADERS:
            await self._respond(
                scope,
                receive,
                send,
                status_code=403,
                detail="sync_proxy_headers_forbidden",
            )
            return

        host_values = [
            value.decode("latin-1").lower()
            for name, value in headers
            if name.lower() == b"host"
        ]
        if len(host_values) != 1 or host_values[0] not in _ALLOWED_HOSTS:
            await self._respond(
                scope,
                receive,
                send,
                status_code=403,
                detail="sync_host_forbidden",
            )
            return

        if scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self._app(scope, receive, send)
            return

        content_lengths = [
            value
            for name, value in headers
            if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await self._invalid_content_length(scope, receive, send)
            return
        if content_lengths:
            try:
                content_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._invalid_content_length(scope, receive, send)
                return
            if content_length < 0:
                await self._invalid_content_length(scope, receive, send)
                return
            if content_length > self._max_body_bytes:
                await self._body_too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(chunk) > self._max_body_bytes - len(body):
                await self._body_too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        sent = False

        async def replay() -> dict[str, object]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await self._app(scope, replay, send)

    async def _invalid_content_length(self, scope, receive, send) -> None:
        await self._respond(
            scope,
            receive,
            send,
            status_code=400,
            detail="sync_invalid_content_length",
        )

    async def _body_too_large(self, scope, receive, send) -> None:
        await self._respond(
            scope,
            receive,
            send,
            status_code=413,
            detail="sync_body_too_large",
        )

    @staticmethod
    async def _respond(
        scope,
        receive,
        send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)


def _identify(
    authenticator: ProjectApiAuthenticator,
    request: Request,
) -> ProjectApiPrincipal:
    try:
        return authenticator.identify(request.headers.get("Authorization"))
    except ProjectAuthenticationError as exc:
        status_code = 503 if str(exc) == "project_api_disabled" else 401
        headers = None if status_code == 503 else {"WWW-Authenticate": "Bearer"}
        raise HTTPException(
            status_code=status_code,
            detail=str(exc),
            headers=headers,
        ) from exc


def _authorize(
    authenticator: ProjectApiAuthenticator,
    principal: ProjectApiPrincipal,
    *,
    project_id: str,
    permission_scope: str | None = None,
) -> None:
    try:
        authenticator.authorize(
            principal,
            project_id=project_id,
            permission_scope=permission_scope,
        )
    except ProjectAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _parse_model(
    request: Request,
    model_type,
):  # type: ignore[no-untyped-def]
    try:
        payload = await request.json()
        return model_type.model_validate(payload)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail="sync_invalid_request",
        ) from exc


def _source_error(exc: ProjectSourceUnavailable) -> HTTPException:
    detail = str(exc)
    if detail not in _SAFE_SOURCE_ERRORS:
        detail = "sync_source_unavailable"
    status_code = 404 if detail == "project_not_synced" else 409
    return HTTPException(status_code=status_code, detail=detail)


def _sync_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ProjectSyncValidationError):
        detail = str(exc)
        if detail not in _SAFE_SYNC_VALIDATION_ERRORS:
            detail = "sync_invalid_request"
        return HTTPException(status_code=400, detail=detail)
    if isinstance(exc, SyncConflict):
        detail = str(exc)
        if detail not in _SAFE_CONFLICT_ERRORS:
            detail = "sync_conflict"
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=500, detail="sync_internal_error")


def _hash_source_id(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def create_sync_app(
    settings: Settings,
    *,
    sync_service=None,
    repository: ProjectSyncRepository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    now = clock or (lambda: datetime.now(UTC))
    if repository is None:
        repository = ProjectSyncRepository(settings.database_path)
        repository.initialize()
    service = sync_service
    if service is None:
        protector = WindowsDpapiProtector()
        repository.configure_protection(
            protection_identity_digest(),
            protector.protector_version,
        )
        service = ProjectSyncService(
            repository,
            protector,
            ProjectSnapshotRegistry(),
            sync_interval_seconds=_SYNC_INTERVAL_SECONDS,
            source_freshness_seconds=settings.project_source_freshness_seconds,
            clock_skew_seconds=settings.project_sync_clock_skew_seconds,
        )
        service.restore_active_projects()
    authenticator = ProjectApiAuthenticator(settings.project_api_principals)
    app = FastAPI(title="XiaoYao Project Sync Gateway", version="0.1.0")
    app.add_middleware(
        _SyncBoundaryMiddleware,
        max_body_bytes=settings.project_sync_max_body_bytes,
    )
    app.state.sync_service = service
    app.state.sync_repository = repository
    app.state.project_api_authenticator = authenticator
    app.state.sync_clock = now

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, object]:
        try:
            repository.initialize()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="sync_not_ready",
            ) from None
        return {"status": "ready", "checks": {"database": "ok"}}

    @app.post("/v1/projects/{project_id}/sync")
    async def apply_sync(project_id: str, request: Request) -> dict[str, object]:
        package = await _parse_model(request, SyncEnvelope)
        if package.project_id != project_id or package.context.project_id != project_id:
            raise HTTPException(status_code=400, detail="sync_project_mismatch")
        principal = _identify(authenticator, request)
        _authorize(authenticator, principal, project_id=project_id)
        try:
            result = service.apply(package, principal=principal, now=now())
        except Exception as exc:
            raise _sync_error(exc) from exc
        return jsonable_encoder(result)

    @app.get("/v1/projects/{project_id}/sync/status")
    def sync_status(project_id: str, request: Request) -> dict[str, object]:
        principal = _identify(authenticator, request)
        _authorize(authenticator, principal, project_id=project_id)
        try:
            status = service.status(project_id, now=now())
        except ProjectSourceUnavailable as exc:
            raise _source_error(exc) from exc
        except Exception as exc:
            raise _sync_error(exc) from exc
        return {"status": jsonable_encoder(status)}

    @app.post("/v1/projects/{project_id}/retrieval-requests")
    async def create_retrieval_request(
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await _parse_model(request, RetrievalRequestCreate)
        principal = _identify(authenticator, request)
        _authorize(authenticator, principal, project_id=project_id)
        for source in body.source_refs:
            _authorize(
                authenticator,
                principal,
                project_id=project_id,
                permission_scope=source.permission_scope,
            )
        current_time = now()
        try:
            service.require_sources_fresh(
                project_id,
                body.source_refs,
                now=current_time,
            )
            retrieval = repository.save_retrieval_request(
                RetrievalRequest(
                    request_id=body.request_id,
                    project_id=project_id,
                    query_hash=body.query_hash,
                    source_id_hashes=tuple(
                        _hash_source_id(source.source_id)
                        for source in body.source_refs
                    ),
                    status=RetrievalRequestStatus.PENDING,
                    created_at=current_time,
                    expires_at=current_time
                    + timedelta(seconds=settings.project_retrieval_ttl_seconds),
                )
            )
        except ProjectSourceUnavailable as exc:
            raise _source_error(exc) from exc
        except (SyncConflict, ValueError) as exc:
            mapped = _sync_error(exc)
            if isinstance(exc, ValueError):
                mapped = HTTPException(
                    status_code=422,
                    detail="sync_invalid_request",
                )
            raise mapped from exc
        except Exception as exc:
            raise _sync_error(exc) from exc
        return JSONResponse(
            status_code=201,
            content={"request": jsonable_encoder(retrieval)},
        )

    @app.get("/v1/projects/{project_id}/retrieval-requests")
    def list_retrieval_requests(
        project_id: str,
        request: Request,
    ) -> dict[str, object]:
        principal = _identify(authenticator, request)
        _authorize(authenticator, principal, project_id=project_id)
        current_time = now()
        results = []
        try:
            for item in repository.list_retrieval_requests(project_id):
                target = None
                if (
                    item.status
                    in {
                        RetrievalRequestStatus.PENDING,
                        RetrievalRequestStatus.IN_PROGRESS,
                    }
                    and item.expires_at <= current_time
                ):
                    target = RetrievalRequestStatus.EXPIRED
                elif item.status is RetrievalRequestStatus.PENDING:
                    target = RetrievalRequestStatus.IN_PROGRESS
                if target is not None:
                    repository.compare_and_set_retrieval_request(
                        project_id,
                        item.request_id,
                        frozenset({item.status}),
                        target,
                    )
                    item = repository.get_retrieval_request(
                        project_id,
                        item.request_id,
                    ) or item
                results.append(item)
        except Exception as exc:
            raise _sync_error(exc) from exc
        return {"requests": jsonable_encoder(results)}

    @app.get("/v1/projects/{project_id}/retrieval-requests/{request_id}")
    def get_retrieval_request(
        project_id: str,
        request_id: str,
        request: Request,
    ) -> dict[str, object]:
        principal = _identify(authenticator, request)
        _authorize(authenticator, principal, project_id=project_id)
        try:
            retrieval = repository.get_retrieval_request(project_id, request_id)
        except Exception as exc:
            raise _sync_error(exc) from exc
        if retrieval is None:
            raise HTTPException(status_code=404, detail="retrieval_request_not_found")
        return {"request": jsonable_encoder(retrieval)}

    return app


def create_default_sync_app() -> FastAPI:
    load_environment_file(LOCAL_ENV_PATH)
    settings = Settings.from_environment()
    repository = ProjectSyncRepository(settings.database_path)
    repository.initialize()
    protector = WindowsDpapiProtector()
    repository.configure_protection(
        protection_identity_digest(),
        protector.protector_version,
    )
    service = ProjectSyncService(
        repository,
        protector,
        ProjectSnapshotRegistry(),
        sync_interval_seconds=_SYNC_INTERVAL_SECONDS,
        source_freshness_seconds=settings.project_source_freshness_seconds,
        clock_skew_seconds=settings.project_sync_clock_skew_seconds,
    )
    service.restore_active_projects()
    return create_sync_app(
        settings,
        repository=repository,
        sync_service=service,
    )
