from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Mapping


_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class ProjectApiPrincipal:
    principal_id: str
    token_sha256: str
    project_ids: frozenset[str]
    permission_scopes: frozenset[str]
    can_review: bool = False

    def __post_init__(self) -> None:
        if _IDENTIFIER_PATTERN.fullmatch(self.principal_id) is None:
            raise ValueError("project API principal_id is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.token_sha256) is None:
            raise ValueError("project API token_sha256 must be lowercase SHA-256")
        if not self.project_ids or any(
            _IDENTIFIER_PATTERN.fullmatch(project_id) is None
            for project_id in self.project_ids
        ):
            raise ValueError("project API project_ids must contain valid project IDs")
        if not self.permission_scopes or any(
            not isinstance(scope, str) or not scope.strip()
            for scope in self.permission_scopes
        ):
            raise ValueError(
                "project API permission_scopes must contain non-blank strings"
            )
        if not isinstance(self.can_review, bool):
            raise ValueError("project API can_review must be boolean")


class ProjectAuthenticationError(RuntimeError):
    pass


class ProjectAuthorizationError(RuntimeError):
    pass


class ProjectApiAuthenticator:
    def __init__(self, principals: tuple[ProjectApiPrincipal, ...]) -> None:
        self._principals = principals

    def authenticate(
        self,
        authorization: str | None,
        *,
        project_id: str,
        permission_scope: str | None = None,
        require_review: bool = False,
    ) -> ProjectApiPrincipal:
        principal = self.identify(authorization)
        self.authorize(
            principal,
            project_id=project_id,
            permission_scope=permission_scope,
            require_review=require_review,
        )
        return principal

    def identify(self, authorization: str | None) -> ProjectApiPrincipal:
        if not self._principals:
            raise ProjectAuthenticationError("project_api_disabled")
        scheme, separator, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or separator != " " or not token:
            raise ProjectAuthenticationError("project_api_authentication_required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        principal = next(
            (
                item
                for item in self._principals
                if hmac.compare_digest(item.token_sha256, digest)
            ),
            None,
        )
        if principal is None:
            raise ProjectAuthenticationError("project_api_authentication_failed")
        return principal

    @staticmethod
    def authorize(
        principal: ProjectApiPrincipal,
        *,
        project_id: str,
        permission_scope: str | None = None,
        require_review: bool = False,
    ) -> None:
        if project_id not in principal.project_ids:
            raise ProjectAuthorizationError("project_access_denied")
        if (
            permission_scope is not None
            and permission_scope not in principal.permission_scopes
        ):
            raise ProjectAuthorizationError("project_scope_denied")
        if require_review and not principal.can_review:
            raise ProjectAuthorizationError("project_review_denied")


def parse_project_api_principals(value: str | None) -> tuple[ProjectApiPrincipal, ...]:
    if value is None or value == "":
        return ()
    try:
        raw = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("COMPANION_PROJECT_API_PRINCIPALS must be valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("COMPANION_PROJECT_API_PRINCIPALS must be a JSON object")
    principals: list[ProjectApiPrincipal] = []
    for principal_id, policy in raw.items():
        if not isinstance(principal_id, str) or not isinstance(policy, Mapping):
            raise ValueError("COMPANION_PROJECT_API_PRINCIPALS has invalid entries")
        project_ids = policy.get("project_ids")
        if not isinstance(project_ids, list) or not all(
            isinstance(item, str) for item in project_ids
        ):
            raise ValueError("project API principal project_ids must be a string list")
        permission_scopes = policy.get("permission_scopes")
        if not isinstance(permission_scopes, list) or not all(
            isinstance(item, str) for item in permission_scopes
        ):
            raise ValueError(
                "project API principal permission_scopes must be a string list"
            )
        principals.append(
            ProjectApiPrincipal(
                principal_id=principal_id,
                token_sha256=policy.get("token_sha256", ""),
                project_ids=frozenset(project_ids),
                permission_scopes=frozenset(permission_scopes),
                can_review=policy.get("can_review", False),
            )
        )
    token_hashes = [item.token_sha256 for item in principals]
    if len(token_hashes) != len(set(token_hashes)):
        raise ValueError("project API token hashes must be unique")
    return tuple(principals)
