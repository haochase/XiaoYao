from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from companion_gateway.device.camera import CameraFrameRegistry
from companion_gateway.vision.service import VisionObservationService


class VisionRuntime(Protocol):
    def describe(self, *, image: bytes, prompt: str) -> str: ...


class VisionRuntimeError(RuntimeError):
    pass


class VisionTurnCoordinator:
    """Bind one completed camera frame to one voice turn and describe it."""

    def __init__(
        self,
        *,
        vision_service: VisionObservationService,
        frame_registry: CameraFrameRegistry,
        runtime: VisionRuntime,
        subject_id: str,
    ) -> None:
        if not subject_id.strip():
            raise ValueError("vision subject_id must not be empty")
        self._vision_service = vision_service
        self._frame_registry = frame_registry
        self._runtime = runtime
        self._subject_id = subject_id

    def describe(self, *, session_id: str, turn_id: str, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("vision prompt must not be empty")
        payload = self._frame_registry.get(
            session_id=session_id,
            turn_id=turn_id,
        )
        if payload is None:
            raise VisionRuntimeError("camera frame is not available")
        self._vision_service.upload(
            subject_id=self._subject_id,
            turn_id=turn_id,
            content_type="image/jpeg",
            payload=payload,
            consent=True,
        )
        try:
            result = self._runtime.describe(image=payload, prompt=prompt).strip()
        except Exception as exc:
            raise VisionRuntimeError("vision model request failed") from exc
        if not result:
            raise VisionRuntimeError("vision model returned empty text")
        self._frame_registry.pop(session_id=session_id, turn_id=turn_id)
        return result

    def describe_and_speak(
        self,
        *,
        session_id: str,
        turn_id: str,
        prompt: str,
        speak: Callable[[str], None],
    ) -> str:
        result = self.describe(
            session_id=session_id,
            turn_id=turn_id,
            prompt=prompt,
        )
        speak(result)
        return result
