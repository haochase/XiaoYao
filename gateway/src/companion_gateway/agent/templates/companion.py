from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from companion_gateway.domain.memory import MemoryProposalCandidate


class CompanionTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: str = Field(min_length=1, max_length=1000)
    emotion: Literal["neutral", "happy", "sad", "anxious", "tired"]
    memory_proposal: MemoryProposalCandidate | None = None
    end_session: bool = False


@dataclass(frozen=True)
class CompanionSession:
    turns: tuple[tuple[str, str], ...] = ()

    def append(self, role: str, content: str) -> "CompanionSession":
        if role not in {"user", "assistant"}:
            raise ValueError("companion role must be user or assistant")
        normalized = content.strip()
        if not normalized:
            raise ValueError("companion turn content must not be empty")
        return CompanionSession(turns=(*self.turns, (role, normalized))[-20:])

    def clear(self) -> "CompanionSession":
        return CompanionSession()


def parse_companion_turn(text: str) -> CompanionTurnResult:
    normalized = text.strip()
    if not normalized:
        raise ValueError("companion response must not be empty")
    candidate = normalized
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[-1][:-3].strip()
    try:
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise ValueError("companion response must be an object")
        return CompanionTurnResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return CompanionTurnResult(reply=normalized, emotion="neutral")


def build_companion_system_prompt(*, max_turns: int) -> str:
    if not 1 <= max_turns <= 10:
        raise ValueError("companion max_turns must be between 1 and 10")
    return (
        "You are XiaoYao in companion mode. Return exactly one JSON object with "
        "reply, emotion, memory_proposal, and end_session. Keep a voice reply to "
        "two or three sentences and ask at most one question. Emotion must be "
        "neutral, happy, sad, anxious, or tired. A stable preference may appear "
        "only as memory_proposal and always requires explicit confirmation before "
        f"storage. End after at most {max_turns} user turns. Never claim that a "
        "memory or action was saved or executed."
    )
