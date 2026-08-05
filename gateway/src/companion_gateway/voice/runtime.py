from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from companion_gateway.audio.bridge import Pcm16Mono


@dataclass(frozen=True)
class ModelResponse:
    text: str
    pcm: Pcm16Mono

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("model response text must not be empty")


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
