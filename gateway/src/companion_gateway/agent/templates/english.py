from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class EnglishCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original: str = Field(min_length=1, max_length=500)
    corrected: str = Field(min_length=1, max_length=500)
    reason_zh: str = Field(min_length=1, max_length=500)


class EnglishScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grammar: int = Field(ge=1, le=5)
    vocabulary: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)


class EnglishTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    heard_text: str = ""
    coach_reply_en: str
    feedback_zh: str = ""
    corrections: tuple[EnglishCorrection, ...] = ()
    scores: EnglishScores | None = None
    suggested_expression: str = ""
    session_complete: bool = False


class EnglishPracticeSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["beginner", "intermediate", "advanced"]
    scenario: Literal["daily", "travel", "cafe", "workplace", "interview"]
    turn_count: int = Field(default=0, ge=0, le=5)
    max_turns: int = Field(default=5, ge=1, le=5)
    corrections: tuple[EnglishCorrection, ...] = ()
    completed: bool = False

    @model_validator(mode="after")
    def validate_turn_limit(self) -> "EnglishPracticeSession":
        if self.turn_count > self.max_turns:
            raise ValueError("turn_count cannot exceed max_turns")
        return self

    def advance(self, result: EnglishTurnResult) -> "EnglishPracticeSession":
        if self.completed:
            return self
        next_count = self.turn_count + 1
        return self.model_copy(
            update={
                "turn_count": next_count,
                "corrections": (*self.corrections, *result.corrections),
                "completed": result.session_complete or next_count >= self.max_turns,
            }
        )


def parse_english_turn(text: str) -> EnglishTurnResult:
    normalized = text.strip()
    if not normalized:
        raise ValueError("English practice response must not be empty")
    candidate = normalized
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[-1][:-3].strip()
    try:
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise ValueError("English response must be an object")
        return EnglishTurnResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return EnglishTurnResult(coach_reply_en=normalized)


def build_english_system_prompt(
    *, level: str, scenario: str, input_mode: str, max_turns: int
) -> str:
    if level not in {"beginner", "intermediate", "advanced"}:
        raise ValueError("unsupported English practice level")
    if scenario not in {"daily", "travel", "cafe", "workplace", "interview"}:
        raise ValueError("unsupported English practice scenario")
    if input_mode not in {"voice", "text"}:
        raise ValueError("input_mode must be voice or text")
    if not 1 <= max_turns <= 5:
        raise ValueError("English max_turns must be between 1 and 5")
    pronunciation_rule = (
        "For text input, you must not provide or imply a pronunciation score."
        if input_mode == "text"
        else "For voice input, give qualitative pronunciation advice only; do not "
        "claim phoneme-level accuracy."
    )
    return (
        "You are XiaoYao, an English speaking coach. Return exactly one JSON object "
        "with heard_text, coach_reply_en, feedback_zh, corrections, scores, "
        "suggested_expression, and session_complete. Scores cover grammar, vocabulary, "
        f"and relevance from 1 to 5. Level={level}; scenario={scenario}; maximum "
        f"turns={max_turns}. {pronunciation_rule}"
    )
