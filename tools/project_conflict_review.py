from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "gateway" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "src"))

from companion_gateway.project.auth import (  # noqa: E402
    ProjectApiPrincipal,
    parse_project_api_principals,
)
from companion_gateway.project.models import (  # noqa: E402
    ConflictCandidate,
    ConflictStatus,
)
from companion_gateway.project.protection import (  # noqa: E402
    ContentProtector,
    WindowsDpapiProtector,
)


PRIVATE_ROOT = Path(r"E:\haochase\xiaoqian\.private\project-review")
ENDPOINT = "http://127.0.0.1:8724"
MAX_RESPONSE_BYTES = 65_536
MAX_POLICY_BYTES = 65_536
MAX_CREDENTIAL_BYTES = 65_536
MAX_TOKEN_BYTES = 8_192
REQUEST_TIMEOUT_SECONDS = 5.0

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class _InvalidCommand(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _InvalidCommand("invalid_command")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _build_direct_opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}), _RejectRedirects())


def _direct_urlopen(request: Request, *, timeout: float) -> object:
    return _build_direct_opener().open(request, timeout=timeout)


def _safe_regular_file(info: os.stat_result, *, max_bytes: int) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & reparse_flag
        and info.st_nlink == 1
        and info.st_size <= max_bytes
    )


def _read_private_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        initial = path.lstat()
        if not _safe_regular_file(initial, max_bytes=max_bytes):
            raise ValueError("private_file_invalid")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _safe_regular_file(opened, max_bytes=max_bytes):
                raise ValueError("private_file_invalid")
            raw = stream.read(max_bytes + 1)
            final_opened = os.fstat(stream.fileno())
        final_path = path.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("private_file_unreadable") from None
    if (
        len(raw) > max_bytes
        or not _safe_regular_file(final_opened, max_bytes=max_bytes)
        or not _safe_regular_file(final_path, max_bytes=max_bytes)
        or not os.path.samestat(initial, opened)
        or not os.path.samestat(initial, final_opened)
        or not os.path.samestat(initial, final_path)
    ):
        raise ValueError("private_file_invalid")
    return raw


def _parse_policy(raw: bytes) -> ProjectApiPrincipal:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("policy_invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValueError("policy_invalid") from None
    if not isinstance(payload, Mapping) or len(payload) != 1:
        raise ValueError("policy_invalid")
    raw_policy = next(iter(payload.values()))
    if (
        not isinstance(raw_policy, Mapping)
        or not isinstance(raw_policy.get("project_ids"), list)
        or len(raw_policy["project_ids"]) != 1
        or not isinstance(raw_policy.get("permission_scopes"), list)
        or len(raw_policy["permission_scopes"]) != 1
    ):
        raise ValueError("policy_invalid")
    try:
        principals = parse_project_api_principals(json.dumps(payload))
    except (TypeError, ValueError):
        raise ValueError("policy_invalid") from None
    if len(principals) != 1:
        raise ValueError("policy_invalid")
    principal = principals[0]
    if (
        not principal.can_review
        or len(principal.project_ids) != 1
        or len(principal.permission_scopes) != 1
    ):
        raise ValueError("policy_invalid")
    return principal


def _load_token(
    private_root: Path,
    protector: ContentProtector,
) -> tuple[ProjectApiPrincipal, bytearray]:
    policy = _parse_policy(
        _read_private_file(
            private_root / "principal.json",
            max_bytes=MAX_POLICY_BYTES,
        )
    )
    project_id = next(iter(policy.project_ids))
    protected = _read_private_file(
        private_root / "credential.dpapi",
        max_bytes=MAX_CREDENTIAL_BYTES,
    )
    try:
        token = bytearray(protector.unprotect(project_id, protected))
    except Exception:
        raise ValueError("credential_invalid") from None
    if not 1 <= len(token) <= MAX_TOKEN_BYTES:
        _wipe(token)
        raise ValueError("credential_invalid")
    digest = hashlib.sha256(token).hexdigest()
    if not hmac.compare_digest(digest, policy.token_sha256):
        _wipe(token)
        raise ValueError("credential_invalid")
    return policy, token


def _wipe(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0


def _decode_json_response(response: object, *, expected_url: str) -> dict[str, Any]:
    with response as stream:  # type: ignore[attr-defined]
        if getattr(stream, "status", None) != 200:
            raise ValueError("response_invalid")
        if stream.geturl() != expected_url:
            raise ValueError("response_redirected")
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("response_invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("response_invalid")
    return payload


def _request_json(
    url: str,
    token: bytearray,
    opener: Callable[..., object],
    *,
    body: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        token_text = token.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("credential_invalid") from None
    if not token_text or any(character.isspace() for character in token_text):
        raise ValueError("credential_invalid")
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token_text}",
    }
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        url,
        data=encoded,
        headers=headers,
        method="GET" if body is None else "POST",
    )
    response = opener(request, timeout=REQUEST_TIMEOUT_SECONDS)
    return _decode_json_response(response, expected_url=url)


def _unique_candidate(
    payload: Mapping[str, Any],
    *,
    project_id: str,
) -> ConflictCandidate:
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or len(conflicts) != 1:
        raise ValueError("review_candidate_not_unique")
    try:
        selected = ConflictCandidate.model_validate(conflicts[0])
    except (TypeError, ValueError):
        raise ValueError("review_candidate_invalid") from None
    if (
        selected.project_id != project_id
        or selected.status is not ConflictStatus.PROPOSED
        or _IDENTIFIER_PATTERN.fullmatch(selected.candidate_id) is None
    ):
        raise ValueError("review_candidate_invalid")
    return selected


def _review_body(
    candidate: ConflictCandidate,
    action: str,
    reason: str,
) -> dict[str, str]:
    if candidate.status is not ConflictStatus.PROPOSED:
        raise ValueError("review_candidate_not_proposed")
    if action not in {"accept", "reject"} or not reason.strip():
        raise ValueError("review_request_invalid")
    return {"action": action, "change_reason": reason}


def _run(
    command: str,
    reason: str | None,
    *,
    policy: ProjectApiPrincipal,
    token: bytearray,
    opener: Callable[..., object],
) -> dict[str, object]:
    project_id = next(iter(policy.project_ids))
    list_url = (
        f"{ENDPOINT}/v1/projects/{project_id}/conflicts?status=proposed"
    )
    selected = _unique_candidate(
        _request_json(list_url, token, opener),
        project_id=project_id,
    )
    if command == "list":
        return {"status": "ready", "candidate_count": 1}
    assert reason is not None
    body = _review_body(selected, command, reason)
    review_url = (
        f"{ENDPOINT}/v1/projects/conflicts/{selected.candidate_id}/review"
    )
    reviewed_payload = _request_json(review_url, token, opener, body=body)
    try:
        reviewed = ConflictCandidate.model_validate(reviewed_payload["candidate"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("review_response_invalid") from None
    expected_status = (
        ConflictStatus.ACCEPTED if command == "accept" else ConflictStatus.REJECTED
    )
    if (
        reviewed.candidate_id != selected.candidate_id
        or reviewed.project_id != project_id
        or reviewed.status is not expected_status
    ):
        raise ValueError("review_response_invalid")
    return {
        "status": "reviewed",
        "action": command,
        "candidate_status": expected_status.value,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(description="Review one fixed local conflict candidate")
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_ArgumentParser,
    )
    commands.add_parser("list")
    for command in ("accept", "reject"):
        review = commands.add_parser(command)
        review.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command != "list" and not args.reason.strip():
        raise _InvalidCommand("invalid_command")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    private_root: Path = PRIVATE_ROOT,
    protector: ContentProtector | None = None,
    opener: Callable[..., object] | None = None,
) -> int:
    try:
        args = _parse_args(argv)
    except _InvalidCommand:
        print(json.dumps({"status": "blocked", "error_type": "invalid_command"}))
        return 2

    token: bytearray | None = None
    try:
        policy, token = _load_token(
            private_root,
            protector or WindowsDpapiProtector(),
        )
        public = _run(
            args.command,
            getattr(args, "reason", None),
            policy=policy,
            token=token,
            opener=opener or _direct_urlopen,
        )
        exit_code = 0
    except Exception:
        public = {"status": "blocked", "error_type": "review_not_ready"}
        exit_code = 1
    finally:
        if token is not None:
            _wipe(token)
    print(json.dumps(public, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
