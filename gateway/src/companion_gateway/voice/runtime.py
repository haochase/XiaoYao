from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.memory import MemoryProposalCandidate
from companion_gateway.domain.models import Identifier, TaskCreate


class VoiceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[
        "acknowledge_medication_occurrence",
        "disable_medication_plan",
    ]
    occurrence_id: Identifier | None = None
    plan_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "VoiceAction":
        if self.type == "acknowledge_medication_occurrence":
            if self.occurrence_id is None or self.plan_id is not None:
                raise ValueError(
                    "acknowledge_medication_occurrence requires occurrence_id only"
                )
        elif self.plan_id is None or self.occurrence_id is not None:
            raise ValueError("disable_medication_plan requires plan_id only")
        return self


class VoiceIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[
        "current_time",
        "current_date",
        "current_datetime",
        "reminder_status",
        "next_meeting",
    ]


@dataclass(frozen=True)
class ModelResponse:
    text: str
    pcm: Pcm16Mono | None
    task: TaskCreate | None = None
    action: VoiceAction | None = None
    intent: VoiceIntent | None = None
    memory_proposals: tuple[MemoryProposalCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip() and self.intent is None:
            raise ValueError("model response text must not be empty")
        if self.pcm is None and self.intent is None:
            raise ValueError("model response pcm is required without an intent")


class ModelRuntime(Protocol):
    def respond(self, pcm: Pcm16Mono) -> ModelResponse: ...


class FakeModelRuntime:
    """Deterministic runtime used for local protocol and audio tests."""

    def __init__(self, *, response_text: str, response_pcm: Pcm16Mono) -> None:
        self._response = ModelResponse(text=response_text, pcm=response_pcm)
        self.received_inputs: list[Pcm16Mono] = []

    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        self.received_inputs.append(pcm)
        return self._response

    def synthesize(self, text: str) -> Pcm16Mono:
        if not text.strip():
            raise ValueError("speech text must not be empty")
        return self._response.pcm
