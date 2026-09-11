from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "gateway" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "src"))

from companion_gateway.project.protection import ContentProtector, WindowsDpapiProtector
from tools.dws_sync import session
from tools.project_conflict_review import (
    PRIVATE_ROOT,
    _direct_urlopen,
    _load_token,
    _request_json,
    _wipe,
)


UPSTREAM = "http://127.0.0.1:8723"
ALLOWED_HOSTS = frozenset({"127.0.0.1:8724", "localhost:8724"})
ALLOWED_ORIGINS = frozenset(f"http://{host}" for host in ALLOWED_HOSTS)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PAGE = ROOT / "gateway" / "static" / "project" / "index.html"
_SESSION_STATUSES = frozenset(
    {"active", "attention_required", "stopped", "expired", "inactive", "failed"}
)
_SESSION_FIELDS = (
    "status",
    "started_at",
    "expires_at",
    "next_due_at",
    "stage",
    "error_type",
    "release_status",
)

Reason = StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject"]
    change_reason: str


def _review_body(payload: dict[str, Any]) -> dict[str, str]:
    try:
        body = ReviewRequest.model_validate(payload)
    except ValueError:
        raise HTTPException(status_code=422, detail="review_request_invalid") from None
    reason = body.change_reason.strip()
    if not reason or len(reason) > 2000:
        raise HTTPException(status_code=422, detail="review_request_invalid")
    return {"action": body.action, "change_reason": reason}


def _proxy(
    path: str,
    *,
    private_root: Path,
    protector: ContentProtector,
    opener: Callable[..., object],
    upstream: str,
    body: dict[str, str] | None = None,
) -> dict[str, Any]:
    token: bytearray | None = None
    try:
        policy, token = _load_token(private_root, protector)
        project_id = next(iter(policy.project_ids))
        payload = _request_json(
            f"{upstream}{path.format(project_id=project_id)}",
            token,
            opener,
            body=body,
        )
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="review_service_unavailable") from None
    finally:
        if token is not None:
            _wipe(token)


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "project_name": str,
        "source_count": int,
        "freshness_seconds": int,
        "last_success_at_present": bool,
        "clock_status": str,
        "active_decision_count": int,
        "proposed_candidate_count": int,
        "rejected_candidate_count": int,
        "accepted_candidate_count": int,
    }
    if any(not isinstance(payload.get(key), value_type) for key, value_type in required.items()):
        raise HTTPException(status_code=503, detail="review_service_unavailable")
    return {key: payload[key] for key in required}


def _conflicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list):
        raise HTTPException(status_code=503, detail="review_service_unavailable")
    fields = (
        "candidate_id",
        "decision_id",
        "active_text",
        "proposed_text",
        "reason",
        "status",
        "created_at",
        "reviewed_by",
        "reviewed_at",
        "review_reason",
        "evidence",
    )
    if any(not isinstance(item, dict) for item in conflicts):
        raise HTTPException(status_code=503, detail="review_service_unavailable")
    return [{field: item.get(field) for field in fields} for item in conflicts]


def _session_payload(result: object) -> dict[str, Any]:
    try:
        payload = result.to_dict()  # type: ignore[attr-defined]
    except Exception:
        raise HTTPException(status_code=503, detail="review_service_unavailable") from None
    if not isinstance(payload, dict) or payload.get("status") not in _SESSION_STATUSES:
        raise HTTPException(status_code=503, detail="review_service_unavailable")
    for field in _SESSION_FIELDS[1:]:
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=503, detail="review_service_unavailable")
    return {field: payload.get(field) for field in _SESSION_FIELDS}


def create_review_app(
    *,
    private_root: Path = PRIVATE_ROOT,
    protector: ContentProtector | None = None,
    opener: Callable[..., object] | None = None,
    upstream: str = UPSTREAM,
    session_root: Path = ROOT,
    session_controller: object = session,
) -> FastAPI:
    if upstream != UPSTREAM:
        raise ValueError("review_upstream_invalid")
    app = FastAPI(title="Project Review", docs_url=None, redoc_url=None, openapi_url=None)
    selected_protector = protector or WindowsDpapiProtector()
    selected_opener = opener or _direct_urlopen

    @app.middleware("http")
    async def restrict_browser(request: Request, call_next):
        if request.headers.get("host") not in ALLOWED_HOSTS:
            return JSONResponse(
                status_code=400, content={"detail": "review_host_invalid"}
            )
        if request.method == "POST" and request.headers.get("origin") not in ALLOWED_ORIGINS:
            return JSONResponse(
                status_code=403, content={"detail": "review_origin_invalid"}
            )
        return await call_next(request)

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(_PAGE, media_type="text/html")

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        return _summary(
            _proxy(
                "/v1/projects/{project_id}/ops/summary",
                private_root=private_root,
                protector=selected_protector,
                opener=selected_opener,
                upstream=upstream,
            )
        )

    @app.get("/api/conflicts")
    def conflicts() -> dict[str, list[dict[str, Any]]]:
        return {
            "conflicts": _conflicts(
                _proxy(
                    "/v1/projects/{project_id}/ops/conflicts",
                    private_root=private_root,
                    protector=selected_protector,
                    opener=selected_opener,
                    upstream=upstream,
                )
            )
        }

    @app.get("/api/session")
    def session_state() -> dict[str, Any]:
        try:
            result = session_controller.session_status(session_root)  # type: ignore[attr-defined]
        except Exception:
            raise HTTPException(
                status_code=503, detail="review_service_unavailable"
            ) from None
        return _session_payload(result)

    @app.post("/api/session/start")
    def session_start() -> dict[str, Any]:
        try:
            result = session_controller.start_session(  # type: ignore[attr-defined]
                session_root, selected_protector
            )
        except Exception:
            raise HTTPException(
                status_code=503, detail="review_service_unavailable"
            ) from None
        return _session_payload(result)

    @app.post("/api/session/stop")
    def session_stop() -> dict[str, Any]:
        try:
            result = session_controller.stop_session(session_root)  # type: ignore[attr-defined]
        except Exception:
            raise HTTPException(
                status_code=503, detail="review_service_unavailable"
            ) from None
        return _session_payload(result)

    @app.post("/api/session/resume")
    def session_resume() -> dict[str, Any]:
        try:
            result = session_controller.resume_session(session_root)  # type: ignore[attr-defined]
        except Exception:
            raise HTTPException(
                status_code=503, detail="review_service_unavailable"
            ) from None
        return _session_payload(result)

    @app.post("/api/conflicts/{candidate_id}/review")
    async def review(candidate_id: str, request: Request) -> dict[str, Any]:
        if _IDENTIFIER.fullmatch(candidate_id) is None:
            raise HTTPException(status_code=422, detail="review_candidate_invalid")
        try:
            request_payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="review_request_invalid") from None
        if not isinstance(request_payload, dict):
            raise HTTPException(status_code=422, detail="review_request_invalid")
        review_body = _review_body(request_payload)
        payload = _proxy(
            f"/v1/projects/conflicts/{candidate_id}/review",
            private_root=private_root,
            protector=selected_protector,
            opener=selected_opener,
            upstream=upstream,
            body=review_body,
        )
        candidate = payload.get("candidate")
        version = payload.get("version")
        expected_status = "accepted" if review_body["action"] == "accept" else "rejected"
        if (
            not isinstance(candidate, dict)
            or candidate.get("candidate_id") != candidate_id
            or candidate.get("status") != expected_status
        ):
            raise HTTPException(status_code=503, detail="review_service_unavailable")
        response: dict[str, Any] = {
            "candidate_id": candidate_id,
            "status": candidate["status"],
        }
        if expected_status == "accepted":
            if not isinstance(version, dict) or type(version.get("version")) is not int:
                raise HTTPException(status_code=503, detail="review_service_unavailable")
            response["version"] = version["version"]
        elif "version" in payload:
            raise HTTPException(status_code=503, detail="review_service_unavailable")
        return response

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_review_app(), host="127.0.0.1", port=8724)
