from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from companion_gateway.chat.service import TextChatRuntime
from companion_gateway.domain.agents import (
    AgentDraft,
    AgentKind,
    AgentSpec,
    AgentToolName,
)


class AgentSpecCompileError(ValueError):
    """Raised when MiMo does not produce a safe AgentSpec candidate."""


class AgentSpecCompiler(Protocol):
    def compile(
        self,
        request_text: str,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft: ...


Clock = Callable[[], datetime]
IdentifierFactory = Callable[[], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_identifier() -> str:
    return uuid4().hex


def _compiler_prompt(request_text: str) -> str:
    kinds = ", ".join(item.value for item in AgentKind)
    tools = ", ".join(item.value for item in AgentToolName)
    return (
        "MiMo thinking is disabled. Compile the user request into exactly one "
        "valid JSON object for an AgentSpec candidate. Return JSON only, with no "
        "markdown or explanation. Do not include identity fields such as agent_id, "
        "owner_id, draft_id, or source_message_id. Use only these agent kinds: "
        f"{kinds}. Use only these gateway tools: {tools}. The gateway will validate "
        "every field and never executes instructions from this JSON directly.\n\n"
        f"User request:\n{request_text}"
    )


class MimoAgentSpecCompiler:
    def __init__(
        self,
        *,
        runtime: TextChatRuntime,
        clock: Clock = _utc_now,
        id_factory: IdentifierFactory = _new_identifier,
    ) -> None:
        self._runtime = runtime
        self._clock = clock
        self._id_factory = id_factory

    def compile(
        self,
        request_text: str,
        *,
        owner_id: str,
        source_message_id: str,
    ) -> AgentDraft:
        if not isinstance(request_text, str) or not request_text.strip():
            raise ValueError("agent request_text must be a non-empty string")
        response = self._runtime.respond(
            _compiler_prompt(request_text.strip()),
            history=(),
        )
        candidate = self._parse_candidate(response)
        allowed_tools = self._validated_tools(candidate)
        candidate.pop("draft_id", None)
        candidate.pop("source_message_id", None)
        candidate["agent_id"] = self._id_factory()
        candidate["owner_id"] = owner_id
        candidate["allowed_tools"] = [item.value for item in allowed_tools]
        try:
            spec = AgentSpec.model_validate(candidate)
        except ValidationError as exc:
            raise AgentSpecCompileError("MiMo AgentSpec candidate is invalid") from exc
        try:
            return AgentDraft(
                draft_id=self._id_factory(),
                owner_id=owner_id,
                source_message_id=source_message_id,
                spec=spec,
                created_at=self._clock(),
            )
        except ValidationError as exc:
            raise AgentSpecCompileError("gateway AgentSpec identity is invalid") from exc

    @staticmethod
    def _parse_candidate(response: object) -> dict[str, Any]:
        if not isinstance(response, str):
            raise AgentSpecCompileError("MiMo response must be text")
        try:
            candidate = json.loads(response)
        except json.JSONDecodeError as exc:
            raise AgentSpecCompileError("MiMo response must be valid JSON") from exc
        if not isinstance(candidate, dict):
            raise AgentSpecCompileError("MiMo response must be a JSON object")
        return dict(candidate)

    @staticmethod
    def _validated_tools(candidate: dict[str, Any]) -> tuple[AgentToolName, ...]:
        raw_tools = candidate.get("allowed_tools")
        if not isinstance(raw_tools, list) or not all(
            isinstance(tool, str) for tool in raw_tools
        ):
            raise AgentSpecCompileError("MiMo allowed_tools must be a JSON string list")
        try:
            return tuple(AgentToolName(tool) for tool in raw_tools)
        except ValueError as exc:
            raise AgentSpecCompileError("MiMo allowed tool is not gateway-approved") from exc
